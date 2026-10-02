"""Node-owned launch and reservation for one bare Studio image child."""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Literal

import httpx
import psutil

from coire_core.models.image_worker import (
    ImageWorkerLoadRequest,
    ImageWorkerLoadResult,
    ImageWorkerProcessConfig,
    ImageWorkerProcessRecord,
    ImageWorkerUnloadRequest,
)
from coire_core.settings import Settings
from coire_node.footprint import resident_bytes as measured_footprint_bytes
from coire_node.image_runtime.bootstrap import (
    ImageWorkerBootstrapError,
    _read_private,
    read_process_config,
)
from coire_node.image_runtime.preflight import verify_image_copy
from coire_node.metrics import (
    ImageNodeOutcome,
    ImageNodeStage,
    image_node_span,
    record_image_stage,
)
from coire_node.store import Store, write_atomic


class ImageProcessUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("image process unavailable")


_STOP_GRACE_S = 4.0
_ProcessState = Literal["same", "gone", "unknown"]


def _process_state(record: ImageWorkerProcessRecord) -> _ProcessState:
    pid = record.status.pid
    created = record.status.process_create_time
    if pid is None or created is None:
        return "unknown"
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return "gone"
        if abs(process.create_time() - created) > 0.01:
            return "gone"
        expected = [
            "-m",
            "coire_node.image_runtime.bootstrap",
            str(record.config.token_file.parent / "launch.json"),
        ]
        return "same" if process.cmdline()[-3:] == expected else "unknown"
    except psutil.NoSuchProcess:
        return "gone"
    except (psutil.Error, OSError, ValueError):
        return "unknown"


def _identity_alive(record: ImageWorkerProcessRecord) -> bool:
    return _process_state(record) == "same"


def _remove_private_state(record_path: Path, record: ImageWorkerProcessRecord) -> None:
    worker_dir = record.config.token_file.parent
    record.config.token_file.unlink(missing_ok=True)
    (worker_dir / "launch.json").unlink(missing_ok=True)
    worker_dir.rmdir()
    record_path.unlink()


def _private_directory(path: Path, *, exclusive: bool = False) -> None:
    path.mkdir(mode=0o700, parents=not exclusive, exist_ok=not exclusive)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ImageProcessUnavailable()


def _private_file(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())


def _child_environment() -> dict[str, str]:
    result = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/var/empty"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "PYTHONNOUSERSITE": "1",
    }
    if temp_dir := os.environ.get("TMPDIR"):
        result["TMPDIR"] = temp_dir
    return result


def _loopback_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class ImageProcessSupervisor:
    """One executor per node with authenticated readiness, adoption and bounded stop."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        other_committed_bytes: Callable[[], int],
        *,
        memory_total_bytes: int | None = None,
        memory_lock: threading.RLock | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.other_committed_bytes = other_committed_bytes
        self.memory_total_bytes = memory_total_bytes or psutil.virtual_memory().total
        self.state_root = Path(settings.node_state_dir) / "image-workers"
        self.record_path = self.state_root / "worker.json"
        self.scratch_root = Path(settings.node_state_dir) / "image-scratch"
        self._lock = memory_lock or threading.RLock()
        self._record: ImageWorkerProcessRecord | None = None
        self._last_stopped: ImageWorkerLoadResult | None = None
        self._uncertain_reserved_bytes = 0

    def committed_bytes(self) -> int:
        with self._lock:
            if self._record is not None:
                return self._record.status.reserved_bytes
            if self.record_path.exists():
                return int(self.memory_total_bytes * self.settings.node_memory_budget_fraction)
            return self._uncertain_reserved_bytes

    def measured_resident_bytes(self) -> int | None:
        """Return the exact live worker's physical footprint, including Metal on macOS."""
        with self._lock:
            record = self._record
            if record is None or record.status.pid is None or not _identity_alive(record):
                return None
            return measured_footprint_bytes(record.status.pid)

    def current_status(self) -> ImageWorkerLoadResult | None:
        with self._lock:
            return self._record.status if self._record is not None else None

    def private_control(self, instance_id: uuid.UUID) -> tuple[ImageWorkerLoadRequest, int, str]:
        """Return private control facts only for this exact ready, live child."""
        with self._lock:
            record = self._record
            if (
                record is None
                or record.status.instance_id != instance_id
                or record.status.state != "ready"
                or not _identity_alive(record)
            ):
                raise ImageProcessUnavailable()
            try:
                config, token = read_process_config(record.config.token_file.parent / "launch.json")
            except (ImageWorkerBootstrapError, OSError, ValueError):
                raise ImageProcessUnavailable() from None
            if config != record.config:
                raise ImageProcessUnavailable()
            return record.config.load, record.config.port, token

    def adopt_from_state(self) -> ImageWorkerLoadResult | None:
        """Re-own only the exact node child; uncertain state keeps a full budget hold."""
        with self._lock:
            if self._record is not None:
                return self._record.status
            if not self.record_path.exists():
                return None
            try:
                record = ImageWorkerProcessRecord.model_validate_json(
                    _read_private(self.record_path, 32 * 1024)
                )
                config_file = record.config.token_file.parent / "launch.json"
                config, _ = read_process_config(config_file)
                if config != record.config or not _identity_alive(record):
                    raise ImageProcessUnavailable()
                verify_image_copy(self.store, record.config.load)
                # A persisted ready state is historical evidence, not current health.
                # Keep the exact process and its hold, but fence dispatch until its
                # authenticated control endpoint proves readiness after adoption.
                record = ImageWorkerProcessRecord(
                    config=record.config,
                    status=record.status.model_copy(update={"state": "starting"}),
                )
                write_atomic(self.record_path, record.model_dump_json().encode("utf-8"))
            except Exception:
                raise ImageProcessUnavailable() from None
            self._record = record
            return record.status

    async def refresh_ready(self, client: httpx.AsyncClient) -> ImageWorkerLoadResult:
        """Promote to ready only after private health proves this exact live process."""
        with self._lock:
            record = self._record
        if record is None or not _identity_alive(record):
            raise ImageProcessUnavailable()
        if record.status.state == "ready":
            return record.status
        try:
            config, token = read_process_config(record.config.token_file.parent / "launch.json")
            if config != record.config:
                raise ImageProcessUnavailable()
            response = await client.get(
                f"http://127.0.0.1:{record.config.port}/health",
                headers={"Authorization": f"Bearer {token}"},
                timeout=2.0,
            )
            if response.status_code != 200:
                return record.status
            candidate = ImageWorkerLoadResult.model_validate(response.json())
        except (httpx.HTTPError, ValueError, OSError, ImageWorkerBootstrapError):
            return record.status
        if (
            candidate.state != "ready"
            or candidate.backend != record.status.backend
            or candidate.instance_id != record.status.instance_id
            or candidate.pid != record.status.pid
            or candidate.process_create_time != record.status.process_create_time
            or candidate.port != record.status.port
            or candidate.reserved_bytes != record.status.reserved_bytes
            or candidate.safe_error is not None
            or not _identity_alive(record)
        ):
            return record.status
        with self._lock:
            if self._record != record or not _identity_alive(record):
                raise ImageProcessUnavailable()
            ready_record = ImageWorkerProcessRecord(config=record.config, status=candidate)
            try:
                write_atomic(self.record_path, ready_record.model_dump_json().encode("utf-8"))
            except OSError:
                raise ImageProcessUnavailable() from None
            self._record = ready_record
        with image_node_span(ImageNodeStage.LOAD):
            record_image_stage(ImageNodeStage.LOAD, ImageNodeOutcome.SUCCEEDED)
        return candidate

    def stop(self, request: ImageWorkerUnloadRequest) -> ImageWorkerLoadResult:
        """TERM then KILL this exact child; release its hold only after confirmed death."""
        started = time.monotonic()
        with image_node_span(ImageNodeStage.CLEANUP):
            try:
                result = self._stop(request)
            except Exception:
                record_image_stage(
                    ImageNodeStage.CLEANUP,
                    ImageNodeOutcome.FAILED,
                    duration_s=time.monotonic() - started,
                )
                raise ImageProcessUnavailable() from None
            record_image_stage(
                ImageNodeStage.CLEANUP,
                ImageNodeOutcome.SUCCEEDED,
                duration_s=time.monotonic() - started,
            )
            return result

    def _stop(self, request: ImageWorkerUnloadRequest) -> ImageWorkerLoadResult:
        with self._lock:
            record = self._record
            if record is None:
                if (
                    self._last_stopped is not None
                    and self._last_stopped.instance_id == request.instance_id
                ):
                    return self._last_stopped
                if not self.record_path.exists():
                    raise ImageProcessUnavailable()
                try:
                    record = ImageWorkerProcessRecord.model_validate_json(
                        _read_private(self.record_path, 32 * 1024)
                    )
                    config_file = record.config.token_file.parent / "launch.json"
                    config, _ = read_process_config(config_file)
                    if (
                        config != record.config
                        or record.config.token_file
                        != self.state_root / str(request.instance_id) / "token"
                        or record.status.instance_id != request.instance_id
                        or _process_state(record) != "gone"
                    ):
                        raise ImageProcessUnavailable()
                    _remove_private_state(self.record_path, record)
                except (ImageWorkerBootstrapError, OSError, ValueError):
                    raise ImageProcessUnavailable() from None
                result = ImageWorkerLoadResult(
                    instance_id=request.instance_id,
                    state="failed",
                    reserved_bytes=0,
                    safe_error="worker_stopped",
                )
                self._uncertain_reserved_bytes = 0
                self._last_stopped = result
                return result
            if record.status.instance_id != request.instance_id:
                raise ImageProcessUnavailable()
            state = _process_state(record)
            if state == "unknown":
                raise ImageProcessUnavailable()
            if state == "same":
                assert record.status.pid is not None
                deadline = time.monotonic() + _STOP_GRACE_S
                with suppress(ProcessLookupError):
                    os.killpg(record.status.pid, signal.SIGTERM)
                state = self._await_death(record, max(time.monotonic(), deadline - 0.25))
                if state == "same":
                    with suppress(ProcessLookupError):
                        os.killpg(record.status.pid, signal.SIGKILL)
                    state = self._await_death(record, deadline)
            if state != "gone":
                raise ImageProcessUnavailable()
            _remove_private_state(self.record_path, record)
            result = ImageWorkerLoadResult(
                instance_id=request.instance_id,
                state="failed",
                reserved_bytes=0,
                safe_error="worker_stopped",
            )
            self._record = None
            self._last_stopped = result
            return result

    @staticmethod
    def _await_death(record: ImageWorkerProcessRecord, deadline: float) -> _ProcessState:
        while True:
            state = _process_state(record)
            # During macOS process exit, identity inspection can briefly fail
            # before the kernel reports death. Keep the hold and keep observing;
            # never signal an unknown identity, but do not abort proof early.
            if state == "gone" or time.monotonic() >= deadline:
                return state
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def start(self, request: ImageWorkerLoadRequest) -> ImageWorkerLoadResult:
        started = time.monotonic()
        with image_node_span(ImageNodeStage.LAUNCH):
            try:
                result = self._start(request)
            except Exception:
                record_image_stage(
                    ImageNodeStage.LAUNCH,
                    ImageNodeOutcome.FAILED,
                    duration_s=time.monotonic() - started,
                )
                raise ImageProcessUnavailable() from None
            record_image_stage(
                ImageNodeStage.LAUNCH,
                ImageNodeOutcome.SUCCEEDED,
                duration_s=time.monotonic() - started,
            )
            return result

    def _start(self, request: ImageWorkerLoadRequest) -> ImageWorkerLoadResult:
        with self._lock:
            if self._record is not None:
                if self._record.config.load == request:
                    return self._record.status
                raise ImageProcessUnavailable()
            if self._uncertain_reserved_bytes or self.record_path.exists():
                raise ImageProcessUnavailable()
            if request.runtime_version != "mflux-0.20.0":
                raise ImageProcessUnavailable()
            verify_image_copy(self.store, request)
            port = self.settings.node_image_worker_port
            low, high = self.settings.engine_port_range
            if low <= port <= high or not _loopback_port_free(port):
                raise ImageProcessUnavailable()
            budget = int(self.memory_total_bytes * self.settings.node_memory_budget_fraction)
            committed = self.other_committed_bytes()
            if committed < 0 or committed + request.reservation_bytes > budget:
                raise ImageProcessUnavailable()
            _private_directory(self.state_root)
            worker_dir = self.state_root / str(request.instance_id)
            _private_directory(worker_dir, exclusive=True)
            proc: subprocess.Popen[bytes] | None = None
            try:
                token_file = worker_dir / "token"
                _private_file(token_file, secrets.token_urlsafe(48).encode("ascii"))
                config = ImageWorkerProcessConfig(
                    load=request,
                    prompt_cache_max_bytes=self.settings.image_prompt_cache_max_bytes,
                    store_dir=Path(self.settings.node_store_dir),
                    scratch_dir=self.scratch_root,
                    token_file=token_file,
                    port=port,
                )
                config_file = worker_dir / "launch.json"
                _private_file(config_file, config.model_dump_json().encode("utf-8"))
                proc = subprocess.Popen(
                    [sys.executable, "-m", "coire_node.image_runtime.bootstrap", str(config_file)],
                    env=_child_environment(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                create_time = psutil.Process(proc.pid).create_time()
                status = ImageWorkerLoadResult(
                    instance_id=request.instance_id,
                    state="starting",
                    pid=proc.pid,
                    process_create_time=create_time,
                    port=port,
                    reserved_bytes=request.reservation_bytes,
                )
                record = ImageWorkerProcessRecord(config=config, status=status)
                write_atomic(self.record_path, record.model_dump_json().encode("utf-8"))
            except Exception:
                if proc is not None:
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except Exception:
                        self._uncertain_reserved_bytes = request.reservation_bytes
                        raise
                shutil.rmtree(worker_dir)
                raise
            self._record = record
            return status
