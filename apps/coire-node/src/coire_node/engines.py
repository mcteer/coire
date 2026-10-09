"""Owning `mlx_lm.server` processes.

Three things here are load-bearing and none of them is obvious.

**Readiness is a generation, not a ping.** `mlx_lm.server` answers `GET /health` with
`{"status": "ok"}` from its HTTP thread the moment it binds, while the model is still loading
on another thread, and it logs nothing when the load completes (research R1). So the agent
polls `/health` only to learn the process is listening, and then issues a one-token completion;
`ready` means *that* succeeded (spec FR-012).

**Engines outlive the agent.** They are spawned with `start_new_session=True`, and the
LaunchDaemon sets `AbandonProcessGroup`, because launchd kills a job's whole process group
when the job dies — a KeepAlive restart would otherwise take every engine with it. On the way
back up the agent re-adopts them by `(pid, create_time)`, since a pid alone is reused
(spec FR-015, research R4).

**Admission uses estimates, not measurements.** A load is refused when committed + estimate
exceeds the budget. Measurements move under load, and an admission decision that is not
reproducible is not a decision (spec FR-020, research R6).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, BinaryIO

import httpx
import psutil
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace

from coire_core.models.adapters import InferenceTarget
from coire_core.models.engine import (
    LIVE_ENGINE_STATES,
    BudgetRefused,
    EngineState,
    EngineStatus,
    ReconcileRequest,
    ReconcileResult,
)
from coire_core.models.registry import EngineBackend
from coire_core.net import shared_http_ssl_context
from coire_core.settings import Settings
from coire_node.footprint import cpu_percent, resident_bytes
from coire_node.store import Store, write_atomic_json

logger = logging.getLogger(__name__)

_meter = otel_metrics.get_meter("coire.node.engines")
_tracer = trace.get_tracer("coire.node.engines")
_load_seconds = _meter.create_histogram(
    "coire_engine_load_seconds", unit="s", description="Time from spawn to first generation."
)
_engine_resident = _meter.create_gauge(
    "coire_engine_resident_bytes", unit="By", description="Measured engine footprint."
)
_adoption_total = _meter.create_counter(
    "coire_engine_adoption_total",
    unit="1",
    description="Exact-process engine re-adoption and generation recheck outcomes.",
)

ENGINES_FILE = "engines.json"
CREATE_TIME_TOLERANCE_S = 1.0
"""psutil reports `create_time()` as float seconds. A second of slack absorbs clock
adjustment without ever matching a different process: pid reuse within one second of the
original's start is not a thing that happens."""
STOP_GRACE_S = 10.0
EXIT_OUTPUT_BYTES = 4096
ENGINE_LOG_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_ENGINE_COMMAND = (sys.executable, "-m", "mlx_lm.server")
VISION_ENGINE_COMMAND = (sys.executable, "-m", "mlx_vlm.server")


class BudgetExceeded(RuntimeError):
    def __init__(self, refusal: BudgetRefused) -> None:
        super().__init__(
            f"needs {refusal.required_bytes} bytes; {refusal.committed_bytes} of "
            f"{refusal.budget_bytes} already committed"
        )
        self.refusal = refusal


class CopyMissing(RuntimeError):
    pass


class BackendMismatch(RuntimeError):
    pass


class NoFreePort(RuntimeError):
    pass


def build_engine_argv(
    *,
    command: list[str],
    model_path: str,
    host: str,
    port: int,
    chat_template_content: str | None = None,
    adapter_path: str | None = None,
) -> list[str]:
    """The exact command line an engine is started with.

    Pure, and separated from spawning, because spec FR-017 is a statement about this list: it
    contains a registry-resolved store path and fixed flags, and nothing that came from a
    request. A test can assert that without patching the process layer.
    """
    argv = [
        *command,
        "--model",
        model_path,
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "INFO",
    ]
    if chat_template_content:
        if len(chat_template_content.encode("utf-8")) > 64 * 1024:
            raise ValueError("chat template exceeds its registry byte bound")
        argv += ["--chat-template", chat_template_content]
    if adapter_path is not None:
        argv += ["--adapter-path", adapter_path]
    return argv


def build_engine_env(base: dict[str, str]) -> dict[str, str]:
    """The environment an engine runs in.

    `mlx_lm.server` honours a per-request `model` field and will download whatever it names,
    with no flag to disable it (research R1). Offline mode makes that impossible even if a
    caller string somehow reached the engine, and the Hugging Face token is removed because an
    engine has no business authenticating to anything.
    """
    env = dict(base)
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    for credential in ("HF_TOKEN", "HF_API_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        env.pop(credential, None)
    env.pop("MLX_TRUST_REMOTE_CODE", None)
    return env


def build_vision_argv(
    *,
    model_path: str,
    host: str,
    port: int,
    vision_cache_size: int = 1,
    max_num_seqs: int = 1,
    max_kv_size: int | None = None,
) -> list[str]:
    """Fixed bare mlx-vlm argv for a registry-resolved local copy."""
    argv = [
        *VISION_ENGINE_COMMAND,
        "--model",
        model_path,
        "--host",
        host,
        "--port",
        str(port),
        "--vision-cache-size",
        str(vision_cache_size),
        "--max-num-seqs",
        str(max_num_seqs),
        "--log-level",
        "INFO",
    ]
    if max_kv_size is not None:
        argv += ["--max-kv-size", str(max_kv_size)]
    return argv


def engine_command(settings: Settings) -> list[str]:
    """The engine's argv prefix.

    `COIRE_ENGINE_COMMAND` (path-separator delimited) selects the fake engine in the Linux CI
    image, where MLX cannot run. Nothing derived from a request ever reaches this.
    """
    override = os.environ.get("COIRE_ENGINE_COMMAND")
    if override:
        return override.split(os.pathsep)
    return list(DEFAULT_ENGINE_COMMAND)


class _Engine:
    """One engine the agent owns."""

    def __init__(
        self,
        *,
        engine_id: uuid.UUID | None,
        slug: str | None,
        port: int,
        estimate_bytes: int,
        pid: int | None = None,
        create_time: float | None = None,
        state: EngineState = EngineState.STARTING,
        started_at: datetime | None = None,
        chat_template_sha256: str | None = None,
        backend: EngineBackend = EngineBackend.MLX_LM,
        stderr_path: Path | None = None,
        target: InferenceTarget | None = None,
        engine_version: str | None = None,
    ) -> None:
        self.engine_id = engine_id
        self.target = target
        self.engine_version = engine_version
        self.slug = slug
        self.port = port
        self.estimate_bytes = estimate_bytes
        self.pid = pid
        self.create_time = create_time
        self.state = state
        self.state_reason: str | None = None
        self.exit_code: int | None = None
        self.exit_output: str | None = None
        self.resident_bytes: int | None = None
        self.cpu_percent: float | None = None
        self.load_seconds: float | None = None
        self.chat_template_sha256 = chat_template_sha256
        self.backend = backend
        self.stderr_path = stderr_path
        self.last_health_at: datetime | None = None
        self.started_at = started_at or datetime.now(UTC)
        self.stopped_at: datetime | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self._psutil: psutil.Process | None = None

    def status(self) -> EngineStatus:
        return EngineStatus(
            engine_id=self.engine_id,
            target=self.target,
            slug=self.slug,
            backend=self.backend,
            port=self.port,
            pid=self.pid,
            process_create_time=self.create_time,
            state=self.state,
            state_reason=self.state_reason,
            exit_code=self.exit_code,
            exit_output=self.exit_output,
            estimate_bytes=self.estimate_bytes,
            resident_bytes=self.resident_bytes,
            resident_delta_bytes=(
                self.resident_bytes - self.estimate_bytes
                if self.resident_bytes is not None and self.estimate_bytes
                else None
            ),
            cpu_percent=self.cpu_percent,
            chat_template_sha256=self.chat_template_sha256,
            load_seconds=self.load_seconds,
            last_health_at=self.last_health_at,
            started_at=self.started_at,
            stopped_at=self.stopped_at,
        )

    def record(self) -> dict[str, Any]:
        return {
            "engine_id": str(self.engine_id) if self.engine_id else None,
            "target": self.target.model_dump(mode="json") if self.target else None,
            "engine_version": self.engine_version,
            "slug": self.slug,
            "port": self.port,
            "pid": self.pid,
            "create_time": self.create_time,
            "estimate_bytes": self.estimate_bytes,
            "backend": self.backend.value,
            "started_at": self.started_at.isoformat(),
            "chat_template_sha256": self.chat_template_sha256,
            "stderr_file": self.stderr_path.name if self.stderr_path is not None else None,
        }


def _alive(pid: int | None, create_time: float | None, *, needle: str | None = None) -> bool:
    """Whether this exact process is still running.

    `(pid, create_time)` together: a pid can be reused, a start time cannot be forged by
    coincidence. `needle` additionally checks the command line still names the expected store
    path, so an unrelated process that inherited the pid is never adopted.
    """
    if pid is None:
        return False
    try:
        proc = psutil.Process(pid)
        if (
            create_time is not None
            and abs(proc.create_time() - create_time) > CREATE_TIME_TOLERANCE_S
        ):
            return False
        if needle is not None and needle not in " ".join(proc.cmdline()):
            return False
        return bool(proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _still_starting(engine: _Engine) -> bool:
    """Read mutable engine state again after a blocking network call."""
    return engine.state is EngineState.STARTING


class EngineManager:
    """Spawns, watches, adopts and stops engines."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        mesh_address: str,
        *,
        memory_lock: threading.RLock | None = None,
        additional_committed_bytes: Callable[[], int] | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._address = mesh_address
        self._engines: dict[str, _Engine] = {}
        self._lock = memory_lock or threading.RLock()
        self._additional_committed_bytes = additional_committed_bytes or (lambda: 0)
        """One lock spanning the budget check *and* the spawn. Two concurrent loads that each
        fit but together do not must not both be admitted (spec edge case 8)."""
        self._state_file = Path(settings.node_state_dir) / ENGINES_FILE
        self._stderr_root = Path(settings.node_state_dir) / "engine-stderr"
        self._stop = threading.Event()
        self._health_thread: threading.Thread | None = None
        self._memory_total = psutil.virtual_memory().total

    # -- budget ------------------------------------------------------------
    def budget_bytes(self) -> int:
        return int(self._memory_total * self._settings.node_memory_budget_fraction)

    def committed_bytes(self) -> int:
        with self._lock:
            return sum(
                e.estimate_bytes or (e.resident_bytes or 0)
                for e in self._engines.values()
                if e.state in LIVE_ENGINE_STATES or e.state is EngineState.ORPHAN
            )

    def _new_stderr_file(self) -> tuple[Path, BinaryIO]:
        self._stderr_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_info = self._stderr_root.lstat()
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_uid != os.getuid()
            or root_info.st_mode & 0o077
        ):
            raise OSError("engine stderr directory is not private")
        path = self._stderr_root / f"{uuid.uuid4().hex}.log"
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_APPEND,
            0o600,
        )
        return path, os.fdopen(fd, "wb", buffering=0)

    def _read_stderr(self, engine: _Engine) -> str:
        path = engine.stderr_path
        if path is None or path.parent != self._stderr_root:
            return ""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    return ""
                os.lseek(fd, max(0, info.st_size - EXIT_OUTPUT_BYTES), os.SEEK_SET)
                return os.read(fd, EXIT_OUTPUT_BYTES).decode("utf-8", "replace")
            finally:
                os.close(fd)
        except OSError:
            return ""

    def _trim_stderr(self, engine: _Engine) -> None:
        path = engine.stderr_path
        if path is None or path.parent != self._stderr_root:
            return
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(fd)
                if (
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == os.getuid()
                    and info.st_mode & 0o077 == 0
                    and info.st_size > ENGINE_LOG_MAX_BYTES
                ):
                    os.ftruncate(fd, 0)
                    logger.warning(
                        "trimmed oversized engine stderr file",
                        extra={"engine_id": str(engine.engine_id)},
                    )
            finally:
                os.close(fd)
        except OSError:
            logger.warning(
                "engine stderr file unavailable", extra={"engine_id": str(engine.engine_id)}
            )

    # -- lifecycle ---------------------------------------------------------
    def start(
        self,
        *,
        engine_id: uuid.UUID,
        slug: str,
        estimate_bytes: int,
        chat_template: str | None = None,
        backend: EngineBackend = EngineBackend.MLX_LM,
        vision_cache_size: int | None = None,
        max_num_seqs: int | None = None,
        max_kv_size: int | None = None,
        target: InferenceTarget | None = None,
    ) -> tuple[bool, EngineStatus]:
        """Start an engine, or return the one already serving this model.

        Returns `(already_running, status)`.
        """
        # Visual preflight hashes the entire verified copy. Keep that I/O out
        # of the lifecycle lock so collection and cancellation remain live
        # while large models are checked. Admission is rechecked below before
        # any process is spawned.
        verified_manifest_sha256: str | None = None
        if backend is EngineBackend.MLX_VLM:
            from coire_node.visual_validation import inspect_local_variant

            manifest = self._store.read_manifest(slug)
            if not self._store.exists(slug) or manifest is None:
                raise CopyMissing(f"no verified copy of {slug} on this node")
            if inspect_local_variant(self._store.path_for(slug)) is not None:
                raise CopyMissing(f"visual copy of {slug} is incomplete or linked")
            if self._store.verify_against(slug, manifest):
                raise CopyMissing(f"visual copy of {slug} differs from its manifest")
            verified_manifest_sha256 = manifest.sha256()
        with self._lock:
            existing = self._engines.get(str(engine_id))
            if existing is not None and (existing.slug != slug or existing.target != target):
                raise BackendMismatch("engine identity already binds another target")
            existing = self._serving(slug, target, engine_id if target else None)
            if existing is not None:
                if existing.backend is not backend:
                    raise BackendMismatch("model is already served by another backend")
                logger.info("%s is already served by engine %s", slug, existing.engine_id)
                return True, existing.status()

            manifest = self._store.read_manifest(slug)
            if not self._store.exists(slug) or manifest is None:
                raise CopyMissing(f"no verified copy of {slug} on this node")
            if (
                verified_manifest_sha256 is not None
                and manifest.sha256() != verified_manifest_sha256
            ):
                raise CopyMissing(f"visual copy of {slug} changed during verification")
            adapter_path = self.adapter_path(target, slug) if target else None
            if adapter_path is not None and backend is not EngineBackend.MLX_LM:
                raise BackendMismatch("adapter serving requires the text backend")
            committed = self.committed_bytes() + self._additional_committed_bytes()
            budget = self.budget_bytes()
            if committed + estimate_bytes > budget:
                raise BudgetExceeded(
                    BudgetRefused(
                        required_bytes=estimate_bytes,
                        committed_bytes=committed,
                        budget_bytes=budget,
                    )
                )

            port = self._allocate_port()
            template_digest = None
            if chat_template:
                self._store.write_template(slug, chat_template)
                template_digest = hashlib.sha256(chat_template.encode()).hexdigest()

            model_path = str(self._store.path_for(slug))
            argv = (
                build_vision_argv(
                    model_path=model_path,
                    host=self._address,
                    port=port,
                    vision_cache_size=vision_cache_size or 1,
                    max_num_seqs=max_num_seqs or 1,
                    max_kv_size=max_kv_size,
                )
                if backend is EngineBackend.MLX_VLM
                else build_engine_argv(
                    command=engine_command(self._settings),
                    model_path=model_path,
                    host=self._address,
                    port=port,
                    chat_template_content=chat_template,
                    adapter_path=str(adapter_path) if adapter_path else None,
                )
            )
            env = build_engine_env(dict(os.environ))
            spawn_version = None
            if backend is EngineBackend.MLX_LM and engine_command(self._settings) == list(
                DEFAULT_ENGINE_COMMAND
            ):
                with contextlib.suppress(PackageNotFoundError):
                    spawn_version = version("mlx-lm")

            if backend is EngineBackend.MLX_VLM:
                with contextlib.suppress(PackageNotFoundError):
                    spawn_version = version("mlx-vlm")

            stderr_path, stderr_file = self._new_stderr_file()
            try:
                proc = subprocess.Popen(
                    argv,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    # Its own session: launchd kills the job's process group on restart, and this
                    # is what keeps the engine out of it (research R4, Apple TN2083).
                    start_new_session=True,
                )
            except Exception:
                stderr_path.unlink(missing_ok=True)
                raise
            finally:
                stderr_file.close()

            engine = _Engine(
                engine_id=engine_id,
                target=target,
                slug=slug,
                port=port,
                estimate_bytes=estimate_bytes,
                pid=proc.pid,
                chat_template_sha256=template_digest,
                backend=backend,
                stderr_path=stderr_path,
                engine_version=spawn_version,
            )
            engine.proc = proc
            with contextlib.suppress(psutil.Error):
                engine.create_time = psutil.Process(proc.pid).create_time()
            # Publish a real footprint before the starting engine becomes visible
            # to health collection. Unknown residency must still fail closed, but
            # a known child should not wait for the next health-loop interval.
            self._sample(engine)
            self._engines[str(engine_id)] = engine
            self._persist()
            logger.info(
                "engine %s starting: %s on port %d (pid %d)", engine_id, slug, port, proc.pid
            )

        threading.Thread(
            target=self._await_ready, args=(str(engine_id),), name=f"ready-{port}", daemon=True
        ).start()
        return False, engine.status()

    def adapter_path(
        self,
        target: InferenceTarget,
        slug: str,
        *,
        verify: bool = True,
    ) -> Path | None:
        """Resolve immutable IDs exclusively inside the verified node stores."""
        manifest = self._store.read_manifest(slug)
        if manifest is None or manifest.sha256() != target.base_manifest_sha256:
            raise CopyMissing("exact base manifest differs from the local copy")
        if target.adapter_id is None:
            return None
        from coire_core.models.training_node import TrainingArtifactManifest
        from coire_node.store import sha256_file

        root = Path(self._settings.node_state_dir) / "training" / "artifacts"
        directory = root / str(target.adapter_id)
        try:
            if any(path.is_symlink() for path in (root.parent, root, directory)):
                raise ValueError("linked artifact store")
            manifest_path = directory / "manifest.json"
            if manifest_path.is_symlink() or manifest_path.stat().st_size > 1024 * 1024:
                raise ValueError("unsafe artifact manifest")
            artifact = TrainingArtifactManifest.model_validate_json(manifest_path.read_bytes())
            if (
                artifact.kind != "adapter"
                or artifact.artifact_id != target.adapter_id
                or artifact.canonical_sha256() != target.adapter_manifest_sha256
            ):
                raise ValueError("artifact identity differs")
            if {entry.name for entry in artifact.files} != {
                "adapters.safetensors",
                "adapter_config.json",
            }:
                raise ValueError("unsupported serving artifact files")
            for entry in artifact.files:
                path = directory / entry.name
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size != entry.bytes
                    or (verify and sha256_file(path) != entry.sha256)
                ):
                    raise ValueError("artifact file differs")
        except (OSError, ValueError) as exc:
            raise CopyMissing("exact adapter artifact is unavailable or differs") from exc
        return directory

    def _serving(
        self,
        slug: str,
        target: InferenceTarget | None = None,
        engine_id: uuid.UUID | None = None,
    ) -> _Engine | None:
        """An engine that is actually serving, or about to.

        `stopping` is deliberately excluded. FR-019 makes a duplicate load a no-op returning
        the existing process, but an engine on its way out is not "already loaded" — returning
        it hands the caller something that becomes `stopped` moments later, which reads as a
        load that failed. A load during a stop starts a fresh engine; both count against the
        budget while they overlap, and draining proper is feature 005.
        """
        for engine in self._engines.values():
            if (
                engine.slug == slug
                and engine.target == target
                and (engine_id is None or engine.engine_id == engine_id)
            ) and engine.state in (
                EngineState.STARTING,
                EngineState.READY,
            ):
                return engine
        return None

    def _allocate_port(self) -> int:
        low, high = self._settings.engine_port_range
        taken = {e.port for e in self._engines.values() if e.state in LIVE_ENGINE_STATES}
        for port in range(low, high + 1):
            if port in taken:
                continue
            # Deliberately without SO_REUSEADDR: the point is to find out whether this port
            # is actually free, and SO_REUSEADDR is precisely the option that lets a bind
            # succeed anyway. With it set, the probe passed and the engine then died with
            # "Address already in use" — a failure attributed to the engine rather than to
            # the allocator that handed it a taken port.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind((self._address, port))
                except OSError:
                    continue
            return port
        raise NoFreePort(f"no free port in {low}-{high}")

    def _await_ready(self, key: str) -> None:
        """Poll until the engine can generate, or until it fails.

        `/health` first, only to learn the socket is up; then a one-token completion, which is
        the thing that actually proves the weights are loaded.
        """
        engine = self._engines.get(key)
        if engine is None:
            return
        if engine.backend is EngineBackend.MLX_VLM:
            with _tracer.start_as_current_span(
                "coire.node.vision.load",
                attributes={"engine_id": str(engine.engine_id), "backend": engine.backend.value},
                record_exception=False,
                set_status_on_exception=False,
            ):
                self._probe_until_ready(engine)
            return
        self._probe_until_ready(engine)

    def _probe_until_ready(self, engine: _Engine) -> None:
        deadline = time.monotonic() + self._settings.node_engine_start_timeout_s
        url = f"http://{self._address}:{engine.port}"
        started = time.monotonic()

        with httpx.Client(timeout=10.0, verify=shared_http_ssl_context()) as client:
            while time.monotonic() < deadline:
                with self._lock:
                    if not _still_starting(engine):
                        return
                if engine.proc is not None and engine.proc.poll() is not None:
                    self._mark_start_failure(engine)
                    return
                if (
                    engine.proc is None
                    and engine.pid is not None
                    and not _alive(engine.pid, engine.create_time)
                ):
                    with self._lock:
                        engine.state = EngineState.FAILED
                        engine.state_reason = "the re-adopted engine process exited"
                        engine.stopped_at = datetime.now(UTC)
                        engine.resident_bytes = None
                        self._persist()
                    _adoption_total.add(1, {"backend": engine.backend.value, "outcome": "failed"})
                    return
                if self._stop.is_set():
                    return
                try:
                    client.get(f"{url}/health")
                except httpx.HTTPError:
                    time.sleep(0.5)
                    continue
                try:
                    payload: dict[str, Any] = {
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 1,
                        "temperature": 0.0,
                    }
                    if engine.target is not None and engine.target.adapter_id is not None:
                        assert engine.slug is not None
                        payload["model"] = str(self._store.path_for(engine.slug))
                        payload["adapters"] = str(
                            self.adapter_path(engine.target, engine.slug, verify=False)
                        )
                    if engine.backend is EngineBackend.MLX_VLM:
                        # mlx-vlm's chat endpoint requires the model field even when
                        # --model already preloaded the local verified store copy.
                        assert engine.slug is not None
                        payload["model"] = str(self._store.path_for(engine.slug))
                    resp = client.post(
                        f"{url}/v1/chat/completions",
                        json=payload,
                        timeout=30.0,
                    )
                except httpx.HTTPError:
                    time.sleep(1.0)
                    continue
                if resp.status_code == 200:
                    with self._lock:
                        if not _still_starting(engine):
                            return
                        engine.state = EngineState.READY
                        engine.load_seconds = time.monotonic() - started
                        engine.last_health_at = datetime.now(UTC)
                        self._sample(engine)
                        self._persist()
                    _load_seconds.record(
                        engine.load_seconds,
                        {"slug": engine.slug or "", "backend": engine.backend.value},
                    )
                    logger.info(
                        "engine %s ready after %.1fs (resident %s bytes vs estimate %s)",
                        engine.engine_id,
                        engine.load_seconds,
                        engine.resident_bytes,
                        engine.estimate_bytes,
                    )
                    if engine.proc is None:
                        _adoption_total.add(
                            1, {"backend": engine.backend.value, "outcome": "ready"}
                        )
                    return
                time.sleep(1.0)

        with self._lock:
            if not _still_starting(engine):
                return
            engine.state = EngineState.FAILED
            engine.state_reason = (
                f"did not answer a generation request within "
                f"{self._settings.node_engine_start_timeout_s:.0f}s"
            )
            engine.stopped_at = datetime.now(UTC)
            self._persist()
        self._terminate(engine)
        if engine.proc is None:
            _adoption_total.add(1, {"backend": engine.backend.value, "outcome": "failed"})

    def _mark_start_failure(self, engine: _Engine) -> None:
        """Record why an engine died during startup, with its own account of it."""
        proc = engine.proc
        output = self._read_stderr(engine)
        if proc is not None:
            engine.exit_code = proc.returncode
        with self._lock:
            engine.state = EngineState.FAILED
            engine.state_reason = f"the engine exited with status {engine.exit_code} during startup"
            engine.exit_output = output or None
            engine.stopped_at = datetime.now(UTC)
            self._persist()
        if engine.stderr_path is not None:
            with contextlib.suppress(OSError):
                engine.stderr_path.unlink(missing_ok=True)
        logger.error(
            "engine %s failed to start (exit %s): %s",
            engine.engine_id,
            engine.exit_code,
            (output or "").strip()[-500:],
        )

    def stop(self, engine_id: uuid.UUID) -> EngineStatus | None:
        with self._lock:
            engine = self._engines.get(str(engine_id))
            if engine is None:
                return None
            engine.state = EngineState.STOPPING
            self._persist()
        threading.Thread(target=self._terminate, args=(engine,), daemon=True).start()
        return engine.status()

    def _terminate(self, engine: _Engine) -> None:
        """SIGTERM the process group, SIGKILL what is left.

        The group, not the pid: `mlx.launch` (feature 006) spawns ranks, and terminating only
        the leader would leave them holding memory.
        """
        pid = engine.pid
        if pid is not None and _alive(pid, engine.create_time):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(pid), 15)
            deadline = time.monotonic() + STOP_GRACE_S
            while time.monotonic() < deadline and _alive(pid, engine.create_time):
                time.sleep(0.2)
            if _alive(pid, engine.create_time):
                logger.warning("engine %s did not exit; killing", engine.engine_id)
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(pid), 9)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and _alive(pid, engine.create_time):
                    time.sleep(0.1)
            if _alive(pid, engine.create_time):
                logger.error("engine %s stop remains uncertain; memory held", engine.engine_id)
                return
        with self._lock:
            if engine.stderr_path is not None:
                with contextlib.suppress(OSError):
                    engine.stderr_path.unlink(missing_ok=True)
            engine.state = EngineState.STOPPED
            engine.stopped_at = datetime.now(UTC)
            engine.resident_bytes = None
            engine.cpu_percent = None
            self._persist()
        logger.info("engine %s stopped", engine.engine_id)

    # -- observation -------------------------------------------------------
    def _sample(self, engine: _Engine) -> None:
        if engine.pid is None:
            return
        engine.resident_bytes = resident_bytes(engine.pid)
        if engine.resident_bytes is not None:
            _engine_resident.set(
                engine.resident_bytes,
                {"slug": engine.slug or "", "backend": engine.backend.value},
            )
        if engine._psutil is None:
            with contextlib.suppress(psutil.Error):
                engine._psutil = psutil.Process(engine.pid)
                engine._psutil.cpu_percent(interval=None)
        elif engine._psutil is not None:
            engine.cpu_percent = cpu_percent(engine._psutil)

    def start_health_loop(self) -> None:
        self._stop.clear()
        self._health_thread = threading.Thread(
            target=self._health_loop, name="engine-health", daemon=True
        )
        self._health_thread.start()

    def _health_loop(self) -> None:
        while not self._stop.wait(self._settings.node_engine_health_interval_s):
            try:
                self.check_once()
            except Exception:
                logger.exception("engine health pass failed")

    def check_once(self) -> None:
        """One health pass: notice deaths, refresh measurements (spec FR-016)."""
        with self._lock:
            for engine in list(self._engines.values()):
                if engine.state not in (
                    EngineState.READY,
                    EngineState.STARTING,
                    EngineState.ORPHAN,
                ):
                    continue
                if not _alive(engine.pid, engine.create_time):
                    if engine.state is EngineState.STARTING:
                        continue  # the ready-probe thread owns this transition
                    engine.state = EngineState.FAILED
                    engine.state_reason = "the engine process exited"
                    engine.stopped_at = datetime.now(UTC)
                    engine.resident_bytes = None
                    logger.error("engine %s died", engine.engine_id)
                    continue
                self._sample(engine)
                self._trim_stderr(engine)
                engine.last_health_at = datetime.now(UTC)
            self._persist()

    def statuses(self) -> list[EngineStatus]:
        """Named `statuses`, not `list`: a method called `list` shadows the builtin inside the
        class body, so every `list[...]` annotation below it silently resolves to the method."""
        with self._lock:
            return [e.status() for e in self._engines.values()]

    def get(self, engine_id: uuid.UUID) -> EngineStatus | None:
        with self._lock:
            engine = self._engines.get(str(engine_id))
            return engine.status() if engine else None

    def attested_engine_version(
        self,
        engine_id: uuid.UUID,
        target: InferenceTarget,
        template_override: str | None = None,
        *,
        backend: EngineBackend | None = None,
    ) -> str | None:
        with self._lock:
            engine = self._engines.get(str(engine_id))
            if (
                engine is None
                or engine.state is not EngineState.READY
                or engine.target != target
                or (backend is not None and engine.backend is not backend)
                or engine.chat_template_sha256
                != (
                    hashlib.sha256(template_override.encode()).hexdigest()
                    if template_override
                    else None
                )
                or not _alive(engine.pid, engine.create_time)
            ):
                return None
            return engine.engine_version

    # -- persistence and adoption -----------------------------------------
    def _persist(self) -> None:
        records = [
            e.record()
            for e in self._engines.values()
            if e.state in LIVE_ENGINE_STATES or e.state is EngineState.ORPHAN
        ]
        with contextlib.suppress(OSError):
            write_atomic_json(self._state_file, records)

    def adopt_from_state(self) -> list[EngineStatus]:
        """Re-own engines recorded before the agent stopped.

        Anything whose `(pid, create_time)` no longer identifies a live process naming this
        store is dropped, not adopted: after a restart the registry asks what is really
        running, and answering with a process that died would be worse than answering nothing.
        """
        adopted: list[EngineStatus] = []
        if not self._state_file.is_file():
            return adopted
        try:
            import json

            records = json.loads(self._state_file.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("engines.json unreadable (%s); starting with none", exc)
            return adopted

        for record in records:
            pid = record.get("pid")
            create_time = record.get("create_time")
            slug = record.get("slug")
            try:
                target = (
                    InferenceTarget.model_validate(record["target"])
                    if record.get("target")
                    else None
                )
                adapter_path = self.adapter_path(target, slug) if target and slug else None
            except (ValueError, CopyMissing):
                logger.warning("exact engine artifact unavailable; leaving process unadopted")
                continue
            needle = str(self._store.path_for(slug)) if slug else None
            if not _alive(pid, create_time, needle=needle):
                logger.warning(
                    "engine %s (pid %s) is gone; not adopting", record.get("engine_id"), pid
                )
                continue
            try:
                argv = psutil.Process(pid).cmdline()
                if needle is not None and argv[argv.index("--model") + 1] != needle:
                    continue
                if adapter_path is not None:
                    if argv[argv.index("--adapter-path") + 1] != str(adapter_path):
                        continue
                elif "--adapter-path" in argv:
                    continue
            except (ValueError, IndexError, psutil.Error):
                continue
            engine = _Engine(
                engine_id=uuid.UUID(record["engine_id"]) if record.get("engine_id") else None,
                slug=slug,
                target=target,
                port=record["port"],
                engine_version=record.get("engine_version"),
                estimate_bytes=record.get("estimate_bytes", 0),
                pid=pid,
                create_time=create_time,
                state=EngineState.STARTING,
                started_at=datetime.fromisoformat(record["started_at"]),
                chat_template_sha256=record.get("chat_template_sha256"),
                backend=EngineBackend(record.get("backend", "mlx_lm")),
                stderr_path=(
                    self._stderr_root / record["stderr_file"]
                    if isinstance(record.get("stderr_file"), str)
                    and len(record["stderr_file"]) == 36
                    and record["stderr_file"].endswith(".log")
                    and all(char in "0123456789abcdef" for char in record["stderr_file"][:-4])
                    else None
                ),
            )
            engine.state_reason = "rechecking generation after an agent restart"
            self._sample(engine)
            key = str(engine.engine_id) if engine.engine_id else f"orphan-{engine.port}"
            self._engines[key] = engine
            adopted.append(engine.status())
            with _tracer.start_as_current_span("coire.node.engine_adopt") as span:
                span.set_attribute("coire.backend", engine.backend.value)
                span.set_attribute("coire.engine_id", str(engine.engine_id))
                _adoption_total.add(1, {"backend": engine.backend.value, "outcome": "rechecking"})
            logger.info("adopted engine %s (pid %s) for %s", engine.engine_id, pid, slug)
            threading.Thread(target=self._probe_until_ready, args=(engine,), daemon=True).start()

        self._persist()
        return adopted

    def find_orphans(self) -> list[EngineStatus]:
        """Engine processes running on this node that the agent does not own."""
        with self._lock:
            known = {e.pid for e in self._engines.values() if e.pid}
        marker = str(self._store.root)
        orphans: list[EngineStatus] = []
        # Inspect every command line, but avoid as_dict()/oneshot metadata assembly
        # for hundreds of unrelated processes on each reconciliation. Exact creation
        # time is needed only after identifying an unowned engine candidate.
        for proc in psutil.process_iter():
            if proc.pid in known:
                continue
            try:
                cmdline = " ".join(proc.cmdline())
            except (psutil.Error, TypeError):
                continue
            if not any(
                marker in cmdline for marker in ("mlx_lm.server", "mlx_vlm.server", "fake_engine")
            ):
                continue
            if marker not in cmdline:
                continue
            port = _port_from(cmdline)
            slug = _slug_from(cmdline, marker)
            try:
                created = proc.create_time()
            except psutil.Error:
                created = None
            engine = _Engine(
                # Give the discovered process a stable control identity immediately. Core must
                # send this same id back when an admin clears the orphan; inventing a different
                # database id there leaves the node unable to find and stop the process.
                engine_id=uuid.uuid4(),
                slug=slug,
                port=port,
                estimate_bytes=0,
                pid=proc.pid,
                create_time=created,
                state=EngineState.ORPHAN,
                backend=(
                    EngineBackend.MLX_VLM if "mlx_vlm.server" in cmdline else EngineBackend.MLX_LM
                ),
            )
            engine.state_reason = "running but not owned by this agent"
            self._sample(engine)
            with self._lock:
                # A concurrent start may have claimed this PID during the inventory scan.
                if any(e.pid == engine.pid for e in self._engines.values()):
                    continue
                if not _alive(engine.pid, engine.create_time, needle=marker):
                    continue
                orphans.append(engine.status())
                self._engines.setdefault(str(engine.engine_id), engine)
        return orphans

    def reconcile(self, request: ReconcileRequest) -> ReconcileResult:
        """Compare what the registry expects against what is running (spec FR-015)."""
        discovered = self.find_orphans()
        with self._lock:
            expected_ids = {str(e.engine_id) for e in request.expected}
            adopted: list[EngineStatus] = []
            dead: list[uuid.UUID] = []

            for expectation in request.expected:
                key = str(expectation.engine_id)
                engine = self._engines.get(key)
                if (
                    engine is not None
                    and _alive(engine.pid, engine.create_time)
                    and (
                        engine.target != expectation.target
                        or engine.slug != expectation.slug
                        or engine.backend != expectation.backend
                    )
                ):
                    engine.state = EngineState.ORPHAN
                    engine.state_reason = "registry expectation differs from the owned exact target"
                    continue
                if (
                    engine is not None
                    and engine.target == expectation.target
                    and engine.slug == expectation.slug
                    and engine.backend == expectation.backend
                    and _alive(engine.pid, engine.create_time)
                ):
                    adopted.append(engine.status())
                    continue
                if engine is not None and engine.state in (
                    EngineState.STOPPED,
                    EngineState.FAILED,
                ):
                    dead.append(expectation.engine_id)
                    continue
                # Expected, but this agent has no live process for it.
                dead.append(expectation.engine_id)
                if engine is not None:
                    engine.state = EngineState.FAILED
                    engine.state_reason = "process gone during agent restart"
                    engine.stopped_at = datetime.now(UTC)

            orphans = [
                e.status()
                for e in self._engines.values()
                if e.state is EngineState.ORPHAN
                or (
                    e.engine_id is not None
                    and str(e.engine_id) not in expected_ids
                    and e.state in LIVE_ENGINE_STATES
                )
            ]
            known_pids = {x.pid for x in orphans}
            orphans.extend(o for o in discovered if o.pid not in known_pids)
            self._persist()
            return ReconcileResult(adopted=adopted, dead=dead, orphans=orphans)

    def shutdown(self) -> None:
        """Stop watching. Engines keep running — that is the point."""
        self._stop.set()
        if self._health_thread is not None:
            self._health_thread.join(timeout=2.0)
        self._persist()


def _port_from(cmdline: str) -> int:
    parts = cmdline.split()
    with contextlib.suppress(ValueError, IndexError):
        return int(parts[parts.index("--port") + 1])
    return 0


def _slug_from(cmdline: str, store_root: str) -> str | None:
    parts = cmdline.split()
    with contextlib.suppress(ValueError, IndexError):
        model = parts[parts.index("--model") + 1]
        if model.startswith(store_root):
            return Path(model).name
    return None


__all__ = [
    "BudgetExceeded",
    "CopyMissing",
    "EngineManager",
    "NoFreePort",
    "build_engine_argv",
    "build_engine_env",
    "engine_command",
]
