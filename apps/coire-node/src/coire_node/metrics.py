"""Node metrics collection (T054).

Collection runs inside a hard budget because the Studios exist to run inference: anything the
agent spends is taken from model decoding. GPU utilisation comes from IOKit's IOAccelerator
statistics, which are readable unprivileged — `powermetrics` gives better numbers but needs a
continuously-running privileged helper, which spec 009 FR-006b forbids (research R7).

Sampling happens on a background thread so a slow `ioreg` never blocks the event loop.
"""

from __future__ import annotations

import logging
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime

import psutil
from opentelemetry import metrics as otel_metrics

from coire_core.models.engine import EngineStatus
from coire_core.models.jobs import JobStatus
from coire_core.models.link import LinkState, RdmaState, StudioDataLinkStatus
from coire_core.models.node import NodePath, NodeStatus, ThermalState

logger = logging.getLogger(__name__)
_meter = otel_metrics.get_meter("coire.node.network")
_data_link_up = _meter.create_gauge("coire_data_link_up", description="Studio data-link IP state")
_data_link_latency = _meter.create_histogram(
    "coire_data_link_latency_ms", unit="ms", description="Studio data-link connect latency"
)
_node_cpu = _meter.create_gauge("coire_node_cpu_percent")
_node_gpu = _meter.create_gauge("coire_node_gpu_percent")
_node_memory_used = _meter.create_gauge("coire_node_memory_used_bytes", unit="By")
_node_memory_budget = _meter.create_gauge("coire_node_memory_budget_bytes", unit="By")
_node_memory_reserved = _meter.create_gauge("coire_node_memory_reserved_bytes", unit="By")
_node_disk_free = _meter.create_gauge("coire_node_disk_free_bytes", unit="By")
_node_process_rss = _meter.create_gauge("coire_node_process_rss_bytes", unit="By")
_node_process_cpu = _meter.create_gauge("coire_node_process_cpu_percent")
_node_collection_duration = _meter.create_histogram("coire_node_collection_duration_ms", unit="ms")
_node_collection_backoff = _meter.create_gauge("coire_node_collection_optional_backoff")
_node_thermal = _meter.create_gauge("coire_node_thermal_state")
_model_load_duration = _meter.create_histogram("coire_model_load_duration_seconds", unit="s")

IOREG_TIMEOUT_S = 3.0
_GPU_UTIL_RE = re.compile(rb'"Device Utilization %"\s*=\s*(\d+)')


def read_gpu_percent() -> float | None:
    """GPU utilisation from IOAccelerator, or None when unavailable.

    Returns None rather than raising or guessing: a missing GPU reading is honest, a
    fabricated one corrupts every capacity decision built on it.
    """
    ioreg = shutil.which("ioreg")
    if ioreg is None:
        return None
    try:
        out = subprocess.run(
            [ioreg, "-r", "-c", "IOAccelerator", "-d", "1", "-w", "0"],
            capture_output=True,
            timeout=IOREG_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("ioreg failed: %s", exc)
        return None
    values = [int(m.group(1)) for m in _GPU_UTIL_RE.finditer(out.stdout)]
    if not values:
        return None
    return float(max(0, min(100, max(values))))


def read_thermal_state() -> ThermalState:
    """Thermal pressure. UNKNOWN when it cannot be read — never guessed as nominal."""
    ioreg = shutil.which("ioreg")
    if ioreg is None:
        return ThermalState.UNKNOWN
    try:
        out = subprocess.run(
            [ioreg, "-r", "-n", "IOPMrootDomain", "-d", "1", "-w", "0"],
            capture_output=True,
            timeout=IOREG_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        return ThermalState.UNKNOWN
    match = re.search(rb'"ThermalPressureLevel"\s*=\s*(\d+)', out.stdout)
    if match is None:
        return ThermalState.UNKNOWN
    return {
        0: ThermalState.NOMINAL,
        10: ThermalState.FAIR,
        20: ThermalState.SERIOUS,
        30: ThermalState.CRITICAL,
    }.get(int(match.group(1)), ThermalState.UNKNOWN)


class MetricsCollector:
    """Samples node and self metrics on a background thread within a configured budget."""

    def __init__(
        self,
        *,
        node_name: str,
        agent_version: str,
        interval_s: float,
        budget_cpu_pct: float,
        budget_rss_bytes: int,
        disk_path: str = "/",
    ) -> None:
        self._name = node_name
        self._version = agent_version
        self._interval = interval_s
        self._budget_cpu = budget_cpu_pct
        self._budget_rss = budget_rss_bytes
        self._disk_path = disk_path
        self._proc = psutil.Process()
        self._started = time.monotonic()
        self._lock = threading.Lock()
        self._latest: NodeStatus | None = None
        # Feature 001 sources, attached by serve(). Absent in unit tests, where the collector
        # is exercised on its own.
        self._store: object | None = None
        self._jobs: object | None = None
        self._engines: object | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sample_number = 0
        self._optional_backoff = 1
        # Prime psutil's CPU deltas so the first real sample is meaningful, not 0.0.
        psutil.cpu_percent(interval=None)
        self._proc.cpu_percent(interval=None)

    def attach(self, *, store: object, jobs: object, engines: object) -> None:
        """Give the collector the feature-001 sources for its additive NodeStatus fields."""
        self._store = store
        self._jobs = jobs
        self._engines = engines

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.sample()
        self._thread = threading.Thread(target=self._loop, name="metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 2)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.sample()
            except Exception:
                logger.exception("metrics sample failed; keeping the previous sample")

    # -- sampling ----------------------------------------------------------
    def sample(self) -> NodeStatus:
        started = time.perf_counter()
        self._sample_number += 1

        vm = psutil.virtual_memory()
        du = psutil.disk_usage(self._disk_path)
        agent_cpu = self._proc.cpu_percent(interval=None)
        agent_rss = self._proc.memory_info().rss

        collect_optional = self._sample_number % self._optional_backoff == 0
        status = NodeStatus(
            name=self._name,
            agent_version=self._version,
            uptime_seconds=time.monotonic() - self._started,
            cpu_percent=float(psutil.cpu_percent(interval=None)),
            gpu_percent=read_gpu_percent() if collect_optional else None,
            thermal_state=read_thermal_state() if collect_optional else ThermalState.UNKNOWN,
            memory_total_bytes=vm.total,
            memory_free_bytes=vm.available,
            disk_total_bytes=du.total,
            disk_free_bytes=du.free,
            agent_cpu_percent=agent_cpu,
            agent_rss_bytes=agent_rss,
            collection_budget_ok=(agent_cpu <= self._budget_cpu and agent_rss <= self._budget_rss),
            collection_duration_ms=0,
            optional_sampling_backoff=self._optional_backoff,
            path=NodePath.MESH,
            sampled_at=datetime.now(UTC),
            engines=self._engine_statuses(),
            jobs=self._job_statuses(),
            memory_budget_bytes=self._budget(),
            memory_committed_bytes=self._committed(),
            store_free_bytes=self._store_free(),
        )

        elapsed = time.perf_counter() - started
        budget_ok = status.collection_budget_ok and elapsed <= self._interval / 2
        if budget_ok:
            self._optional_backoff = max(1, self._optional_backoff // 2)
        else:
            self._optional_backoff = min(16, self._optional_backoff * 2)
        status = status.model_copy(
            update={
                "collection_budget_ok": budget_ok,
                "collection_duration_ms": elapsed * 1000,
                "optional_sampling_backoff": self._optional_backoff,
            }
        )
        attrs = {"node": self._name}
        _node_cpu.set(status.cpu_percent, attrs)
        if status.gpu_percent is not None:
            _node_gpu.set(status.gpu_percent, attrs)
        _node_memory_used.set(status.memory_total_bytes - status.memory_free_bytes, attrs)
        _node_memory_budget.set(status.memory_budget_bytes, attrs)
        _node_memory_reserved.set(status.memory_committed_bytes, attrs)
        _node_disk_free.set(status.disk_free_bytes, attrs)
        _node_collection_duration.record(status.collection_duration_ms, attrs)
        _node_collection_backoff.set(status.optional_sampling_backoff, attrs)
        _node_thermal.set(1, {**attrs, "state": status.thermal_state.value})
        for engine in status.engines[:64]:
            engine_attrs = {"node": self._name, "model_id": engine.slug or "unknown"}
            if engine.resident_bytes is not None:
                _node_process_rss.set(engine.resident_bytes, engine_attrs)
            if engine.cpu_percent is not None:
                _node_process_cpu.set(engine.cpu_percent, engine_attrs)
            if engine.load_seconds is not None:
                _model_load_duration.record(engine.load_seconds, engine_attrs)
        if elapsed > self._interval / 2:
            logger.warning(
                "metrics collection took %.2fs against a %.1fs interval; "
                "consider a longer interval rather than competing with inference",
                elapsed,
                self._interval,
            )
        if not status.collection_budget_ok:
            logger.warning(
                "collection budget exceeded: cpu=%.1f%% (limit %.1f%%) rss=%dMiB (limit %dMiB)",
                agent_cpu,
                self._budget_cpu,
                agent_rss // 1024 // 1024,
                self._budget_rss // 1024 // 1024,
            )

        with self._lock:
            self._latest = status
        return status

    # -- feature 001 sources ----------------------------------------------
    #
    # Each is defensive: a fault in one source must degrade that field, never take out the
    # whole health response. A node that stops answering /node/health looks unreachable to the
    # control plane, and losing a node because its job list raised is a bad trade.
    def _engine_statuses(self) -> list[EngineStatus]:
        if self._engines is None:
            return []
        try:
            return list(self._engines.statuses())  # type: ignore[attr-defined]
        except Exception:
            logger.exception("could not read engine statuses")
            return []

    def _job_statuses(self) -> list[JobStatus]:
        if self._jobs is None:
            return []
        try:
            return list(self._jobs.active())  # type: ignore[attr-defined]
        except Exception:
            logger.exception("could not read job statuses")
            return []

    def _budget(self) -> int:
        if self._engines is None:
            return 0
        try:
            return int(self._engines.budget_bytes())  # type: ignore[attr-defined]
        except Exception:
            return 0

    def _committed(self) -> int:
        if self._engines is None:
            return 0
        try:
            return int(self._engines.committed_bytes())  # type: ignore[attr-defined]
        except Exception:
            return 0

    def _store_free(self) -> int:
        if self._store is None:
            return 0
        try:
            return int(self._store.free_bytes())  # type: ignore[attr-defined]
        except Exception:
            return 0

    def latest(self, *, path: NodePath = NodePath.MESH) -> NodeStatus:
        with self._lock:
            status = self._latest
        if status is None:
            status = self.sample()
        return status.model_copy(update={"path": path})

    def data_link_status(self, *, port: int = 9401) -> StudioDataLinkStatus:
        """Measure IP and RDMA independently; control reachability is never inferred here."""
        peer = "coire-edge-b" if self._name == "coire-edge-a" else "coire-edge-a"
        started = time.perf_counter()
        ip_state = LinkState.DOWN
        reason: str | None = None
        try:
            with socket.create_connection((f"{peer}.fabric", port), timeout=1.0):
                ip_state = LinkState.UP
        except OSError as exc:
            reason = str(exc)[:512]
        latency_ms = (time.perf_counter() - started) * 1000 if ip_state is LinkState.UP else None
        attributes = {"network_path": "data", "peer": peer, "node": self._name}
        _data_link_up.set(1 if ip_state is LinkState.UP else 0, attributes)
        if latency_ms is not None:
            _data_link_latency.record(latency_ms, attributes)

        rdma_state = RdmaState.UNKNOWN
        profiler = shutil.which("system_profiler")
        if profiler:
            try:
                result = subprocess.run(
                    [profiler, "SPThunderboltDataType"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                text = result.stdout.lower()
                if "rdma" in text and ("yes" in text or "enabled" in text):
                    rdma_state = RdmaState.UP
                elif result.returncode == 0 and "thunderbolt" in text:
                    rdma_state = RdmaState.DEGRADED
            except (OSError, subprocess.SubprocessError):
                pass
        return StudioDataLinkStatus(
            node_a="coire-edge-a",
            node_b="coire-edge-b",
            ip_state=ip_state,
            rdma_state=rdma_state,
            latency_ms=latency_ms,
            measured_at=datetime.now(UTC),
            reason=reason,
        )
