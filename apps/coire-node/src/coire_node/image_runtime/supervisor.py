"""Node-owned launch and reservation for one bare Studio image child."""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import psutil

from coire_core.models.image_worker import (
    ImageWorkerLoadRequest,
    ImageWorkerLoadResult,
    ImageWorkerProcessConfig,
    ImageWorkerProcessRecord,
)
from coire_core.settings import Settings
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


def _identity_alive(record: ImageWorkerProcessRecord) -> bool:
    pid = record.status.pid
    created = record.status.process_create_time
    if pid is None or created is None:
        return False
    try:
        process = psutil.Process(pid)
        expected = [
            "-m",
            "coire_node.image_runtime.bootstrap",
            str(record.config.token_file.parent / "launch.json"),
        ]
        return bool(
            process.is_running()
            and abs(process.create_time() - created) <= 0.01
            and process.cmdline()[-3:] == expected
        )
    except (psutil.Error, OSError, ValueError):
        return False


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
    """One executor/node; later slices add readiness, adoption and stop control."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        other_committed_bytes: Callable[[], int],
        *,
        memory_total_bytes: int | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.other_committed_bytes = other_committed_bytes
        self.memory_total_bytes = memory_total_bytes or psutil.virtual_memory().total
        self.state_root = Path(settings.node_state_dir) / "image-workers"
        self.record_path = self.state_root / "worker.json"
        self.scratch_root = Path(settings.node_state_dir) / "image-scratch"
        self._lock = threading.RLock()
        self._record: ImageWorkerProcessRecord | None = None
        self._uncertain_reserved_bytes = 0

    def committed_bytes(self) -> int:
        with self._lock:
            if self._record is not None:
                return self._record.status.reserved_bytes
            if self.record_path.exists():
                return int(self.memory_total_bytes * self.settings.node_memory_budget_fraction)
            return self._uncertain_reserved_bytes

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
