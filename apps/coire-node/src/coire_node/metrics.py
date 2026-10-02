"""Node metrics collection (T054).

Collection runs inside a hard budget because the Studios exist to run inference: anything the
agent spends is taken from model decoding. GPU utilisation comes from IOKit's IOAccelerator
statistics, which are readable unprivileged — `powermetrics` gives better numbers but needs a
continuously-running privileged helper, which spec 009 FR-006b forbids (research R7).

Sampling happens on a background thread so a slow `ioreg` never blocks the event loop.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import math
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum

import psutil
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace
from opentelemetry.trace import Span

from coire_core.models.engine import EngineStatus
from coire_core.models.jobs import JobStatus
from coire_core.models.link import LinkState, RdmaState, StudioDataLinkStatus
from coire_core.models.node import NodePath, NodeStatus, ThermalState
from coire_core.models.registry import EngineBackend

logger = logging.getLogger(__name__)
_meter = otel_metrics.get_meter("coire.node.network")
_data_link_up = _meter.create_gauge("coire_data_link_up", description="Studio data-link IP state")
_data_link_latency = _meter.create_histogram(
    "coire_data_link_latency_ms", unit="ms", description="Studio data-link connect latency"
)

_image_meter = otel_metrics.get_meter("coire.node.image")
_image_tracer = trace.get_tracer("coire.node.image")
image_stages_total = _image_meter.create_counter(
    "coire_image_node_stages_total", unit="1", description="Studio image stage outcomes"
)
image_stage_seconds = _image_meter.create_histogram(
    "coire_image_node_stage_seconds", unit="s", description="Studio image stage duration"
)
image_cache_bytes = _image_meter.create_gauge(
    "coire_image_node_cache_bytes", unit="By", description="Studio image cache occupancy"
)
image_cache_events = _image_meter.create_counter(
    "coire_image_node_cache_events_total",
    unit="1",
    description="Studio image cache hits, misses and evictions",
)


class ImageNodeStage(StrEnum):
    JOURNAL = "journal"
    STATUS = "status"
    CANCEL = "cancel"
    LAUNCH = "launch"
    LOAD = "load"
    PREPROCESS = "preprocess"
    GENERATE = "generate"
    UPSCALE = "upscale"
    CLASSIFY = "classify"
    TRANSFER = "transfer"
    CLEANUP = "cleanup"


class ImageNodeOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def record_image_cache(stage: str, event: str, occupancy_bytes: int) -> None:
    """Record cache traffic without prompt, owner or source-image labels."""
    if stage not in {"prompt", "control"} or event not in {"hit", "miss", "eviction", "store"}:
        raise ValueError("unknown image cache label")
    if occupancy_bytes < 0:
        raise ValueError("negative image cache occupancy")
    if event != "store":
        image_cache_events.add(1, attributes={"stage": stage, "event": event})
    image_cache_bytes.set(occupancy_bytes, attributes={"stage": stage})


def record_image_stage(
    stage: ImageNodeStage,
    outcome: ImageNodeOutcome,
    *,
    duration_s: float | None = None,
    job_id: str | None = None,
) -> None:
    """Only fixed stage/outcome labels reach node metrics."""
    if not isinstance(stage, ImageNodeStage):
        raise ValueError("unknown image stage")
    if not isinstance(outcome, ImageNodeOutcome):
        raise ValueError("unknown image outcome")
    if duration_s is not None and (not math.isfinite(duration_s) or duration_s < 0):
        raise ValueError("invalid image stage duration")
    if job_id is not None and re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", job_id) is None:
        raise ValueError("invalid image job identifier")
    attributes = {"stage": stage.value, "outcome": outcome.value}
    image_stages_total.add(1, attributes=attributes)
    if duration_s is not None:
        image_stage_seconds.record(duration_s, attributes=attributes)
    logger.info(
        "image stage",
        extra={"image_stage": stage.value, "image_outcome": outcome.value, "job_id": job_id},
    )


@contextmanager
def image_node_span(stage: ImageNodeStage, *, job_id: str | None = None) -> Iterator[Span]:
    """Start a fixed-name Studio image span with an optional validated ULID."""
    if not isinstance(stage, ImageNodeStage):
        raise ValueError("unknown image stage")
    if job_id is not None and re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", job_id) is None:
        raise ValueError("invalid image job identifier")
    with _image_tracer.start_as_current_span(f"coire.node.image.{stage.value}") as span:
        if job_id is not None:
            span.set_attribute("job_id", job_id)
        yield span


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
        self._os_version = platform.mac_ver()[0] or platform.release()
        try:
            self._engine_version = importlib.metadata.version("mlx-lm")
        except importlib.metadata.PackageNotFoundError:
            self._engine_version = "unavailable"
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
        self._image_workers: object | None = None
        self._reservations: object | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Prime psutil's CPU deltas so the first real sample is meaningful, not 0.0.
        psutil.cpu_percent(interval=None)
        self._proc.cpu_percent(interval=None)

    def attach(
        self,
        *,
        store: object,
        jobs: object,
        engines: object,
        image_workers: object | None = None,
        reservations: object | None = None,
    ) -> None:
        """Give the collector disjoint node memory holders for NodeStatus."""
        self._store = store
        self._jobs = jobs
        self._engines = engines
        self._image_workers = image_workers
        self._reservations = reservations

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

        vm = psutil.virtual_memory()
        du = psutil.disk_usage(self._disk_path)
        agent_cpu = self._proc.cpu_percent(interval=None)
        agent_rss = self._proc.memory_info().rss
        # psutil's first non-blocking sample can overshoot the physical 100% ceiling by a
        # fraction while the collector itself wakes. Treat that measurement jitter as the
        # saturated boundary only; real budgets below 100% remain strict.
        cpu_budget_ok = agent_cpu <= self._budget_cpu or (
            self._budget_cpu >= 100.0 and agent_cpu <= 100.5
        )

        status = NodeStatus(
            name=self._name,
            agent_version=self._version,
            os_version=self._os_version,
            engine_version=self._engine_version,
            uptime_seconds=time.monotonic() - self._started,
            cpu_percent=float(psutil.cpu_percent(interval=None)),
            gpu_percent=read_gpu_percent(),
            thermal_state=read_thermal_state(),
            memory_total_bytes=vm.total,
            memory_free_bytes=vm.available,
            disk_total_bytes=du.total,
            disk_free_bytes=du.free,
            agent_cpu_percent=agent_cpu,
            agent_rss_bytes=agent_rss,
            collection_budget_ok=(cpu_budget_ok and agent_rss <= self._budget_rss),
            path=NodePath.MESH,
            sampled_at=datetime.now(UTC),
            engines=self._engine_statuses(),
            jobs=self._job_statuses(),
            memory_budget_bytes=self._budget(),
            memory_committed_bytes=self._committed(),
            image_worker_resident_bytes=self._image_resident(),
            store_free_bytes=self._store_free(),
            supported_backends=(
                [EngineBackend.MLX_LM, EngineBackend.MLX_VLM]
                if importlib.util.find_spec("mlx_vlm") is not None
                else [EngineBackend.MLX_LM]
            ),
        )

        elapsed = time.perf_counter() - started
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
            committed = int(self._engines.committed_bytes())  # type: ignore[attr-defined]
            if self._image_workers is not None:
                committed += int(self._image_workers.committed_bytes())  # type: ignore[attr-defined]
            if self._reservations is not None:
                committed += int(self._reservations.held_bytes())  # type: ignore[attr-defined]
            return committed
        except Exception:
            logger.exception("could not read complete node memory commitment")
            return self._budget() or int(psutil.virtual_memory().total)

    def _store_free(self) -> int:
        if self._store is None:
            return 0
        try:
            return int(self._store.free_bytes())  # type: ignore[attr-defined]
        except Exception:
            return 0

    def _image_resident(self) -> int | None:
        if self._image_workers is None:
            return None
        try:
            return self._image_workers.measured_resident_bytes()  # type: ignore[attr-defined, no-any-return]
        except Exception:
            logger.exception("could not measure image worker physical footprint")
            return None

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
