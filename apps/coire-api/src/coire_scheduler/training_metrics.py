"""Read-only persisted training baseline, independently scheduled even when disabled.

No node/network calls or engine imports. Stop-proof rows, not command receipts or
job terminal states, clear process uncertainty and guard violations.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from opentelemetry import trace
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    TrainingAttemptRow,
    TrainingCheckpointRow,
    TrainingCommandRow,
    TrainingEventRow,
    TrainingJobRow,
    TrainingMetricRow,
    TrainingParticipantRow,
    session_scope,
)
from coire_api.polling import wait_or_stop
from coire_api.training.telemetry import (
    GUARD_REASONS,
    TRAINING_STATES,
    TrainingBaselineMetrics,
    TrainingMetricSnapshot,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.scheduler.training")


@dataclass(frozen=True)
class AttemptObservation:
    job_id: str
    state: str
    state_since: datetime
    created_at: datetime
    unknown: bool
    lease_expires_at: datetime
    last_update_at: datetime | None
    stop_requested_at: datetime | None
    pause_requested_at: datetime | None
    pause_reason: str | None
    current: bool = True


def reduce_training_metrics(
    *,
    now: datetime,
    jobs: dict[str, int],
    attempts: Sequence[AttemptObservation],
    recoveries: Sequence[datetime],
    pending_checkpoints: Sequence[datetime],
) -> TrainingMetricSnapshot:
    """Oldest unresolved age wins; retries, other jobs and validation cannot mask it."""

    def age(start: datetime) -> float:
        return max(0.0, (now - start).total_seconds())

    progress = [
        age(a.last_update_at or a.state_since)
        for a in attempts
        if a.state == "running" and a.current and not a.unknown
    ]
    recovery = [age(t) for t in recoveries]
    guards = dict.fromkeys(GUARD_REASONS, 0)
    for a in attempts:
        if a.unknown:
            # The schema has no ownership-unknown transition timestamp. Attempt
            # creation is a conservative durable bound, never a poll-time reset.
            recovery.append(age(a.created_at))
        if age(a.lease_expires_at) > 0:
            guards["lease_expired"] += 1
        cancel_since = a.stop_requested_at
        if a.state == "cancelling":
            cancel_since = min(a.state_since, cancel_since) if cancel_since else a.state_since
        if cancel_since is not None and age(cancel_since) > 5:
            guards["cancel"] += 1
        pause_since = a.pause_requested_at
        if a.state == "pausing":
            pause_since = min(a.state_since, pause_since) if pause_since else a.state_since
        if pause_since is not None and age(pause_since) > 60:
            reason = a.pause_reason if a.pause_reason in GUARD_REASONS else "cancel"
            guards[reason] += 1
    return TrainingMetricSnapshot(
        now.timestamp(),
        {s: jobs.get(s, 0) for s in TRAINING_STATES},
        max(progress, default=0.0),
        max(recovery, default=0.0),
        max((age(t) for t in pending_checkpoints), default=0.0),
        guards,
    )


async def load_training_metrics(session: AsyncSession) -> TrainingMetricSnapshot:
    """Use a fresh session: first statement establishes a consistent read-only snapshot."""
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    now = await session.scalar(select(func.now()))
    assert isinstance(now, datetime)
    jobs = dict(
        (
            await session.execute(
                select(TrainingJobRow.state, func.count())
                .where(TrainingJobRow.deleted_at.is_(None))
                .group_by(TrainingJobRow.state)
            )
        )
        .tuples()
        .all()
    )
    # Find the earliest retained event in the current continuous state segment;
    # repeated same-state events and progress must not reset recovery/guard age.
    event_state = TrainingEventRow.payload["state"].as_string()
    transitions = (
        select(
            TrainingEventRow.job_id.label("job_id"),
            func.max(TrainingEventRow.sequence).label("sequence"),
        )
        .join(TrainingJobRow, TrainingJobRow.id == TrainingEventRow.job_id)
        .where(
            event_state.is_not(None),
            event_state != TrainingJobRow.state,
        )
        .group_by(TrainingEventRow.job_id)
        .subquery()
    )
    since = (
        select(
            TrainingEventRow.job_id.label("job_id"),
            func.min(TrainingEventRow.occurred_at).label("since"),
        )
        .join(
            TrainingJobRow,
            TrainingJobRow.id == TrainingEventRow.job_id,
        )
        .outerjoin(transitions, transitions.c.job_id == TrainingEventRow.job_id)
        .where(
            event_state == TrainingJobRow.state,
            TrainingEventRow.sequence > func.coalesce(transitions.c.sequence, 0),
        )
        .group_by(TrainingEventRow.job_id)
        .subquery()
    )
    state_since = func.coalesce(since.c.since, TrainingJobRow.updated_at)
    recoveries = list(
        (
            await session.scalars(
                select(state_since)
                .select_from(TrainingJobRow)
                .outerjoin(since, since.c.job_id == TrainingJobRow.id)
                .where(TrainingJobRow.state == "recovering", TrainingJobRow.deleted_at.is_(None))
            )
        ).all()
    )
    progress = (
        select(
            TrainingMetricRow.attempt_id.label("attempt_id"),
            func.max(TrainingMetricRow.recorded_at).label("at"),
        )
        .where(
            TrainingMetricRow.kind == "train",
            TrainingMetricRow.completed_update > 0,
            TrainingMetricRow.rolled_back.is_(False),
        )
        .group_by(TrainingMetricRow.attempt_id)
        .subquery()
    )
    controls = (
        select(
            TrainingCommandRow.attempt_id.label("attempt_id"),
            func.min(TrainingCommandRow.created_at)
            .filter(TrainingCommandRow.operation == "node.training.stop")
            .label("stop_at"),
            func.min(TrainingCommandRow.created_at)
            .filter(TrainingCommandRow.operation == "node.training.pause")
            .label("pause_at"),
        )
        .where(TrainingCommandRow.operation.in_(["node.training.stop", "node.training.pause"]))
        .group_by(TrainingCommandRow.attempt_id)
        .subquery()
    )
    # A stopped-at field without its immutable proof is not enough. Missing rank
    # rows also retain uncertainty; old fenced attempts remain visible until death.
    proofs = (
        select(TrainingParticipantRow.attempt_id.label("attempt_id"), func.count().label("count"))
        .where(
            TrainingParticipantRow.stopped_at.is_not(None),
            TrainingParticipantRow.stop_proof.is_not(None),
        )
        .group_by(TrainingParticipantRow.attempt_id)
        .subquery()
    )
    rows = (
        (
            await session.execute(
                select(
                    TrainingJobRow.id,
                    TrainingJobRow.state,
                    state_since,
                    TrainingAttemptRow.created_at,
                    TrainingAttemptRow.state,
                    TrainingAttemptRow.lease_expires_at,
                    progress.c.at,
                    controls.c.stop_at,
                    controls.c.pause_at,
                    TrainingJobRow.safe_reason,
                    TrainingAttemptRow.fence,
                    TrainingJobRow.fence,
                )
                .join(TrainingAttemptRow, TrainingAttemptRow.job_id == TrainingJobRow.id)
                .outerjoin(since, since.c.job_id == TrainingJobRow.id)
                .outerjoin(progress, progress.c.attempt_id == TrainingAttemptRow.id)
                .outerjoin(controls, controls.c.attempt_id == TrainingAttemptRow.id)
                .outerjoin(proofs, proofs.c.attempt_id == TrainingAttemptRow.id)
                .where(func.coalesce(proofs.c.count, 0) < TrainingAttemptRow.world_size)
            )
        )
        .tuples()
        .all()
    )
    attempts = [
        AttemptObservation(
            job_id=row[0],
            state=row[1],
            state_since=max(row[2], row[3]),
            created_at=row[3],
            unknown=row[4] in {"unknown", "stopped"} or row[10] != row[11],
            lease_expires_at=row[5],
            last_update_at=row[6],
            stop_requested_at=row[7],
            pause_requested_at=row[8],
            pause_reason=row[9],
            current=row[10] == row[11],
        )
        for row in rows
    ]
    checkpoints = list(
        (
            await session.scalars(
                select(TrainingCheckpointRow.created_at)
                .join(TrainingJobRow, TrainingJobRow.id == TrainingCheckpointRow.job_id)
                .join(TrainingAttemptRow, TrainingAttemptRow.id == TrainingCheckpointRow.attempt_id)
                .outerjoin(proofs, proofs.c.attempt_id == TrainingAttemptRow.id)
                .where(
                    TrainingCheckpointRow.state.in_(["staging", "replicating"]),
                    TrainingCheckpointRow.committed_at.is_(None),
                    TrainingCheckpointRow.purged_at.is_(None),
                    # Retained incomplete lineage is no longer an active transfer
                    # once abandoned and every producing rank is proven dead.
                    # Terminal/fenced metadata alone never clears uncertainty.
                    ~and_(
                        or_(
                            TrainingJobRow.state.in_(["succeeded", "failed", "cancelled"]),
                            TrainingCheckpointRow.fence != TrainingJobRow.fence,
                        ),
                        func.coalesce(proofs.c.count, 0) >= TrainingAttemptRow.world_size,
                    ),
                )
            )
        ).all()
    )
    return reduce_training_metrics(
        now=now,
        jobs=jobs,
        attempts=attempts,
        recoveries=recoveries,
        pending_checkpoints=checkpoints,
    )


async def poll_training_metrics(
    stop: asyncio.Event,
    *,
    interval_s: float = 5.0,
    emitter: TrainingBaselineMetrics | None = None,
) -> None:
    """Scheduler lifespan task; caller owns stop event and awaits it at shutdown.

    Run regardless of TRAINING_ENABLED. No DBOS workflow or node worker needed.
    Failures preserve last successful values/timestamp and log no SQL/error text.
    """
    if not 0 < interval_s <= 15:
        raise ValueError("training metrics interval must be positive and at most 15 seconds")
    output = emitter or TrainingBaselineMetrics()
    while not stop.is_set():
        with tracer.start_as_current_span(
            "coire.scheduler.training.metrics.refresh",
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                async with session_scope() as session:
                    snapshot = await load_training_metrics(session)
                output.publish(snapshot)
            except Exception:
                logger.warning(
                    "training baseline refresh failed", extra={"operation": "metrics.refresh"}
                )
            else:
                logger.debug("training baseline refreshed", extra={"operation": "metrics.refresh"})
        await wait_or_stop(stop, interval_s)
