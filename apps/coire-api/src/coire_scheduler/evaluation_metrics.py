"""Durable evaluation baseline, independent of admission and optional diagnostics."""

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions, Meter, Observation
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import EvaluationRunRow, session_scope
from coire_api.polling import wait_or_stop
from coire_core.models.evaluation import EvaluationState

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.scheduler.evaluation")


@dataclass(frozen=True)
class EvaluationMetricSnapshot:
    timestamp: float
    runs: Mapping[str, int]
    cleanup_overdue: float
    pending_oldest: float


class EvaluationBaselineMetrics:
    def __init__(self, meter: Meter | None = None) -> None:
        self.snapshot: EvaluationMetricSnapshot | None = None
        output = meter or metrics.get_meter("coire.scheduler.evaluation")
        for name, field, unit in (
            ("coire_evaluation_snapshot_timestamp", "timestamp", "s"),
            ("coire_evaluation_runs", "runs", ""),
            ("coire_evaluation_cleanup_overdue", "cleanup_overdue", "s"),
            ("coire_evaluation_pending_oldest", "pending_oldest", "s"),
        ):
            output.create_observable_gauge(name, callbacks=[self.callback(field)], unit=unit)

    def callback(self, field: str) -> Callable[[CallbackOptions | None], Iterable[Observation]]:
        def observe(options: CallbackOptions | None) -> Iterable[Observation]:
            if self.snapshot is None:
                return ()
            if field == "runs":
                return tuple(
                    Observation(self.snapshot.runs.get(state.value, 0), {"state": state.value})
                    for state in EvaluationState
                )
            return (Observation(getattr(self.snapshot, field)),)

        return observe

    def publish(self, snapshot: EvaluationMetricSnapshot) -> None:
        self.snapshot = snapshot


async def load_evaluation_metrics(
    session: AsyncSession, *, now: datetime | None = None
) -> EvaluationMetricSnapshot:
    observed = now or datetime.now(UTC)
    counts = (
        await session.execute(
            select(EvaluationRunRow.state, func.count()).group_by(EvaluationRunRow.state)
        )
    ).all()
    cleanup = await session.scalar(
        select(func.min(EvaluationRunRow.execution_deadline_at)).where(
            EvaluationRunRow.cleanup_state != "complete"
        )
    )
    pending = await session.scalar(
        select(func.min(EvaluationRunRow.created_at)).where(EvaluationRunRow.state == "queued")
    )
    by_state: dict[str, int] = {}
    for key, count in counts:
        by_state[key] = count
    return EvaluationMetricSnapshot(
        observed.timestamp(),
        {state.value: by_state.get(state.value, 0) for state in EvaluationState},
        max(0.0, (observed - cleanup).total_seconds()) if cleanup else 0.0,
        max(0.0, (observed - pending).total_seconds()) if pending else 0.0,
    )


async def poll_evaluation_metrics(
    stop: asyncio.Event, *, emitter: EvaluationBaselineMetrics | None = None
) -> None:
    output = emitter or EvaluationBaselineMetrics()
    while not stop.is_set():
        with tracer.start_as_current_span(
            "coire.scheduler.evaluation.metrics.refresh",
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                async with session_scope() as session:
                    value = await load_evaluation_metrics(session)
                output.publish(value)
            except Exception:
                logger.warning(
                    "evaluation baseline refresh failed", extra={"operation": "metrics.refresh"}
                )
        await wait_or_stop(stop, 5.0)
