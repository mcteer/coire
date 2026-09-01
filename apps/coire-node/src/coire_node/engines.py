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
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psutil
from opentelemetry import metrics as otel_metrics

from coire_core.models.engine import (
    LIVE_ENGINE_STATES,
    BudgetRefused,
    EngineState,
    EngineStatus,
    ReconcileRequest,
    ReconcileResult,
)
from coire_core.settings import Settings
from coire_node.footprint import cpu_percent, resident_bytes
from coire_node.store import Store, write_atomic_json

logger = logging.getLogger(__name__)

_meter = otel_metrics.get_meter("coire.node.engines")
_load_seconds = _meter.create_histogram(
    "coire_engine_load_seconds", unit="s", description="Time from spawn to first generation."
)
_engine_resident = _meter.create_gauge(
    "coire_engine_resident_bytes", unit="By", description="Measured engine footprint."
)
_unload_seconds = _meter.create_histogram(
    "coire_engine_unload_seconds", unit="s", description="Time from stop request to exit."
)

ENGINES_FILE = "engines.json"
CREATE_TIME_TOLERANCE_S = 1.0
"""psutil reports `create_time()` as float seconds. A second of slack absorbs clock
adjustment without ever matching a different process: pid reuse within one second of the
original's start is not a thing that happens."""
STOP_GRACE_S = 10.0
EXIT_OUTPUT_BYTES = 4096
DEFAULT_ENGINE_COMMAND = (sys.executable, "-m", "mlx_lm.server")


class BudgetExceeded(RuntimeError):
    def __init__(self, refusal: BudgetRefused) -> None:
        super().__init__(
            f"needs {refusal.required_bytes} bytes; {refusal.committed_bytes} of "
            f"{refusal.budget_bytes} already committed"
        )
        self.refusal = refusal


class CopyMissing(RuntimeError):
    pass


class NoFreePort(RuntimeError):
    pass


def build_engine_argv(
    *,
    command: list[str],
    model_path: str,
    host: str,
    port: int,
    chat_template_path: str | None = None,
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
    if chat_template_path:
        argv += ["--chat-template", chat_template_path]
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
    env.pop("HF_TOKEN", None)
    return env


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
    ) -> None:
        self.engine_id = engine_id
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
        self.last_health_at: datetime | None = None
        self.started_at = started_at or datetime.now(UTC)
        self.stopped_at: datetime | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self._psutil: psutil.Process | None = None

    def status(self) -> EngineStatus:
        return EngineStatus(
            engine_id=self.engine_id,
            slug=self.slug,
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
            "slug": self.slug,
            "port": self.port,
            "pid": self.pid,
            "create_time": self.create_time,
            "estimate_bytes": self.estimate_bytes,
            "started_at": self.started_at.isoformat(),
            "chat_template_sha256": self.chat_template_sha256,
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


class EngineManager:
    """Spawns, watches, adopts and stops engines."""

    def __init__(self, settings: Settings, store: Store, mesh_address: str) -> None:
        self._settings = settings
        self._store = store
        self._address = mesh_address
        self._engines: dict[str, _Engine] = {}
        self._lock = threading.RLock()
        """One lock spanning the budget check *and* the spawn. Two concurrent loads that each
        fit but together do not must not both be admitted (spec edge case 8)."""
        self._state_file = Path(settings.node_state_dir) / ENGINES_FILE
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

    # -- lifecycle ---------------------------------------------------------
    def start(
        self,
        *,
        engine_id: uuid.UUID,
        slug: str,
        estimate_bytes: int,
        chat_template: str | None = None,
    ) -> tuple[bool, EngineStatus]:
        """Start an engine, or return the one already serving this model.

        Returns `(already_running, status)`.
        """
        with self._lock:
            existing = self._serving(slug)
            if existing is not None:
                logger.info("%s is already served by engine %s", slug, existing.engine_id)
                return True, existing.status()

            if not self._store.exists(slug) or self._store.read_manifest(slug) is None:
                raise CopyMissing(f"no verified copy of {slug} on this node")

            committed = self.committed_bytes()
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
            template_path = None
            if chat_template:
                template_path = str(self._store.write_template(slug, chat_template))
                template_digest = hashlib.sha256(chat_template.encode()).hexdigest()

            argv = build_engine_argv(
                command=engine_command(self._settings),
                model_path=str(self._store.path_for(slug)),
                host=self._address,
                port=port,
                chat_template_path=template_path,
            )
            env = build_engine_env(dict(os.environ))

            proc = subprocess.Popen(
                argv,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                # Its own session: launchd kills the job's process group on restart, and this
                # is what keeps the engine out of it (research R4, Apple TN2083).
                start_new_session=True,
            )

            engine = _Engine(
                engine_id=engine_id,
                slug=slug,
                port=port,
                estimate_bytes=estimate_bytes,
                pid=proc.pid,
                chat_template_sha256=template_digest,
            )
            engine.proc = proc
            with contextlib.suppress(psutil.Error):
                engine.create_time = psutil.Process(proc.pid).create_time()
            self._engines[str(engine_id)] = engine
            self._persist()
            logger.info(
                "engine %s starting: %s on port %d (pid %d)", engine_id, slug, port, proc.pid
            )

        threading.Thread(
            target=self._await_ready, args=(str(engine_id),), name=f"ready-{port}", daemon=True
        ).start()
        return False, engine.status()

    def _serving(self, slug: str) -> _Engine | None:
        """An engine that is actually serving, or about to.

        `stopping` is deliberately excluded. FR-019 makes a duplicate load a no-op returning
        the existing process, but an engine on its way out is not "already loaded" — returning
        it hands the caller something that becomes `stopped` moments later, which reads as a
        load that failed. A load during a stop starts a fresh engine; both count against the
        budget while they overlap, and draining proper is feature 005.
        """
        for engine in self._engines.values():
            if engine.slug == slug and engine.state in (
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
        deadline = time.monotonic() + self._settings.node_engine_start_timeout_s
        url = f"http://{self._address}:{engine.port}"
        started = time.monotonic()

        with httpx.Client(timeout=10.0) as client:
            while time.monotonic() < deadline:
                if engine.proc is not None and engine.proc.poll() is not None:
                    self._mark_start_failure(engine)
                    return
                if self._stop.is_set():
                    return
                try:
                    client.get(f"{url}/health")
                except httpx.HTTPError:
                    time.sleep(0.5)
                    continue
                try:
                    resp = client.post(
                        f"{url}/v1/chat/completions",
                        json={
                            "messages": [{"role": "user", "content": "hi"}],
                            "max_tokens": 1,
                            "temperature": 0.0,
                        },
                        timeout=30.0,
                    )
                except httpx.HTTPError:
                    time.sleep(1.0)
                    continue
                if resp.status_code == 200:
                    with self._lock:
                        engine.state = EngineState.READY
                        engine.load_seconds = time.monotonic() - started
                        engine.last_health_at = datetime.now(UTC)
                        self._sample(engine)
                        self._persist()
                    _load_seconds.record(engine.load_seconds, {"slug": engine.slug or ""})
                    logger.info(
                        "engine %s ready after %.1fs (resident %s bytes vs estimate %s)",
                        engine.engine_id,
                        engine.load_seconds,
                        engine.resident_bytes,
                        engine.estimate_bytes,
                    )
                    return
                time.sleep(1.0)

        with self._lock:
            engine.state = EngineState.FAILED
            engine.state_reason = (
                f"did not answer a generation request within "
                f"{self._settings.node_engine_start_timeout_s:.0f}s"
            )
            engine.stopped_at = datetime.now(UTC)
            self._persist()
        self._terminate(engine)

    def _mark_start_failure(self, engine: _Engine) -> None:
        """Record why an engine died during startup, with its own account of it."""
        proc = engine.proc
        output = ""
        if proc is not None:
            engine.exit_code = proc.returncode
            if proc.stderr is not None:
                with contextlib.suppress(Exception):
                    output = proc.stderr.read().decode("utf-8", "replace")[-EXIT_OUTPUT_BYTES:]
        with self._lock:
            engine.state = EngineState.FAILED
            engine.state_reason = f"the engine exited with status {engine.exit_code} during startup"
            engine.exit_output = output or None
            engine.stopped_at = datetime.now(UTC)
            self._persist()
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
        started = time.monotonic()
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
        with self._lock:
            engine.state = EngineState.STOPPED
            engine.stopped_at = datetime.now(UTC)
            engine.resident_bytes = None
            engine.cpu_percent = None
            self._persist()
        _unload_seconds.record(time.monotonic() - started, {"slug": engine.slug or ""})
        logger.info("engine %s stopped", engine.engine_id)

    # -- observation -------------------------------------------------------
    def _sample(self, engine: _Engine) -> None:
        if engine.pid is None:
            return
        engine.resident_bytes = resident_bytes(engine.pid)
        if engine.resident_bytes is not None:
            _engine_resident.set(engine.resident_bytes, {"slug": engine.slug or ""})
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
            needle = str(self._store.path_for(slug)) if slug else None
            if not _alive(pid, create_time, needle=needle):
                logger.warning(
                    "engine %s (pid %s) is gone; not adopting", record.get("engine_id"), pid
                )
                continue
            engine = _Engine(
                engine_id=uuid.UUID(record["engine_id"]) if record.get("engine_id") else None,
                slug=slug,
                port=record["port"],
                estimate_bytes=record.get("estimate_bytes", 0),
                pid=pid,
                create_time=create_time,
                state=EngineState.READY,
                started_at=datetime.fromisoformat(record["started_at"]),
                chat_template_sha256=record.get("chat_template_sha256"),
            )
            engine.state_reason = "re-adopted after an agent restart"
            self._sample(engine)
            key = str(engine.engine_id) if engine.engine_id else f"orphan-{engine.port}"
            self._engines[key] = engine
            adopted.append(engine.status())
            logger.info("adopted engine %s (pid %s) for %s", engine.engine_id, pid, slug)

        self._persist()
        return adopted

    def find_orphans(self) -> list[EngineStatus]:
        """Engine processes running on this node that the agent does not own."""
        known = {e.pid for e in self._engines.values() if e.pid}
        marker = str(self._store.root)
        orphans: list[EngineStatus] = []
        for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
            except (psutil.Error, TypeError):
                continue
            if proc.info["pid"] in known:
                continue
            if "mlx_lm.server" not in cmdline and "fake_engine" not in cmdline:
                continue
            if marker not in cmdline:
                continue
            port = _port_from(cmdline)
            slug = _slug_from(cmdline, marker)
            engine = _Engine(
                # Give the discovered process a stable control identity immediately. Core must
                # send this same id back when an admin clears the orphan; inventing a different
                # database id there leaves the node unable to find and stop the process.
                engine_id=uuid.uuid4(),
                slug=slug,
                port=port,
                estimate_bytes=0,
                pid=proc.info["pid"],
                create_time=proc.info.get("create_time"),
                state=EngineState.ORPHAN,
            )
            engine.state_reason = "running but not owned by this agent"
            self._sample(engine)
            orphans.append(engine.status())
            self._engines.setdefault(str(engine.engine_id), engine)
        return orphans

    def reconcile(self, request: ReconcileRequest) -> ReconcileResult:
        """Compare what the registry expects against what is running (spec FR-015)."""
        with self._lock:
            expected_ids = {str(e.engine_id) for e in request.expected}
            adopted: list[EngineStatus] = []
            dead: list[uuid.UUID] = []

            for expectation in request.expected:
                key = str(expectation.engine_id)
                engine = self._engines.get(key)
                if engine is not None and _alive(engine.pid, engine.create_time):
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
            orphans.extend(o for o in self.find_orphans() if o.pid not in known_pids)
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
