"""Content-free SFT telemetry; resource IDs are correlation fields, never metric labels."""

import logging
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions, Meter, Observation

tracer = trace.get_tracer("coire.api.training")
meter = metrics.get_meter("coire.api.training")
event_count = meter.create_counter("coire_training_events_total", unit="{event}")
metric_count = meter.create_counter("coire_training_metric_samples_total", unit="{sample}")
refused_observations = meter.create_counter(
    "coire_training_observations_refused_total", unit="{observation}"
)
logger = logging.getLogger("coire.api.training")
operations = meter.create_counter("coire_training_operations_total", unit="{operation}")
P = ParamSpec("P")
R = TypeVar("R")

TRAINING_STATES = (
    "queued",
    "preflighting",
    "reserving",
    "running",
    "pausing",
    "paused",
    "recovering",
    "finalizing",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
)
GUARD_REASONS = ("memory_breach", "latency_breach", "thermal_breach", "lease_expired", "cancel")


@dataclass(frozen=True)
class TrainingMetricSnapshot:
    timestamp: float
    jobs: Mapping[str, int]
    progress_oldest: float
    recovery_oldest: float
    checkpoint_pending_oldest: float
    guard_overdue: Mapping[str, int]


class TrainingBaselineMetrics:
    """Publish one immutable snapshot; failed polls never manufacture a healthy zero.

    Construct only in the scheduler, after telemetry configuration. Until the first
    successful poll callbacks emit no series, so the baseline-unavailable alert fires.
    Seconds use OTel's canonical `s` unit (exported as `_seconds` by Prometheus).
    """

    def __init__(self, metric_meter: Meter | None = None) -> None:
        self.snapshot: TrainingMetricSnapshot | None = None
        selected = metric_meter or metrics.get_meter("coire.scheduler.training")
        for name, field, unit in (
            ("coire_training_snapshot_timestamp", "timestamp", "s"),
            ("coire_training_jobs", "jobs", ""),
            ("coire_training_progress_oldest", "progress_oldest", "s"),
            ("coire_training_recovery_oldest", "recovery_oldest", "s"),
            ("coire_training_checkpoint_pending_oldest", "checkpoint_pending_oldest", "s"),
            ("coire_training_guard_overdue", "guard_overdue", ""),
        ):
            selected.create_observable_gauge(name, callbacks=[self._callback(field)], unit=unit)

    def _callback(self, field: str) -> Callable[[CallbackOptions], Iterable[Observation]]:
        def observe(_options: CallbackOptions) -> Iterable[Observation]:
            snapshot = self.snapshot
            if snapshot is None:
                return ()
            if field == "jobs":
                return tuple(
                    Observation(snapshot.jobs.get(s, 0), {"state": s}) for s in TRAINING_STATES
                )
            if field == "guard_overdue":
                return tuple(
                    Observation(snapshot.guard_overdue.get(r, 0), {"reason": r})
                    for r in GUARD_REASONS
                )
            return (Observation(getattr(snapshot, field)),)

        return observe

    def publish(self, snapshot: TrainingMetricSnapshot) -> None:
        self.snapshot = snapshot


def observed(
    name: str,
) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
    """Async spans suppress arbitrary exception text, including SQL/source contents."""

    def decorate(
        function: Callable[P, Coroutine[Any, Any, R]],
    ) -> Callable[P, Coroutine[Any, Any, R]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with tracer.start_as_current_span(
                name, record_exception=False, set_status_on_exception=False
            ):
                try:
                    result = await function(*args, **kwargs)
                except Exception:
                    operations.add(1, {"operation": name, "outcome": "refused"})
                    logger.info("training operation refused", extra={"operation": name})
                    raise
                operations.add(1, {"operation": name, "outcome": "completed"})
                return result

        return wrapped

    return decorate
