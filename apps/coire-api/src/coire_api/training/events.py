"""Transactional ordered progress, fenced metrics and content-free reconnect snapshots."""

from __future__ import annotations

import binascii
import uuid
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import TrainingAttemptRow, TrainingEventRow, TrainingJobRow, TrainingMetricRow
from coire_api.training.telemetry import (
    event_count,
    logger,
    metric_count,
    refused_observations,
    tracer,
)
from coire_core.errors import TrainingConflict, TrainingNotFound, TrainingValidationError
from coire_core.models.training import (
    TrainingEvent,
    TrainingEventPayload,
    TrainingJobSnapshot,
    TrainingMetricCursor,
    TrainingMetricPage,
    TrainingMetricSample,
    TrainingProgressEvent,
    TrainingReplayPage,
)


async def current_job(session: AsyncSession, job_id: str, *, lock: bool = False) -> TrainingJobRow:
    row = await session.get(TrainingJobRow, job_id, populate_existing=True, with_for_update=lock)
    if row is None or row.deleted_at is not None:
        raise TrainingNotFound()
    return row


async def current_attempt(
    session: AsyncSession, job: TrainingJobRow, attempt_id: str, fence: int
) -> TrainingAttemptRow:
    attempt = await session.get(
        TrainingAttemptRow, attempt_id, populate_existing=True, with_for_update=True
    )
    if (
        attempt is None
        or attempt.job_id != job.id
        or attempt.fence != fence
        or job.fence != fence
        or attempt.state not in {"running", "stopping"}
        or job.state not in {"running", "pausing"}
        or attempt.lease_expires_at <= datetime.now(UTC)
    ):
        refused_observations.add(1, {"reason": "stale_attempt"})
        raise TrainingConflict("Training observation does not belong to the live attempt")
    return attempt


def project_event(row: TrainingEventRow) -> TrainingEvent:
    return TrainingEvent.model_validate(
        {
            "id": row.sequence,
            "job_id": row.job_id,
            "attempt_id": row.attempt_id,
            "state_version": row.state_version,
            "occurred_at": row.occurred_at,
            "kind": row.payload["kind"],
            "payload": row.payload,
        }
    )


async def append_event(
    session: AsyncSession,
    job_id: str,
    payload: TrainingEventPayload,
    *,
    attempt_id: str | None = None,
    fence: int | None = None,
) -> TrainingEvent:
    with tracer.start_as_current_span("coire.api.training.event.append") as span:
        span.set_attribute("coire.job_id", job_id)
        job = await current_job(session, job_id, lock=True)
        if (attempt_id is None) != (fence is None):
            raise TrainingValidationError("Attempt and fence must be supplied together")
        if attempt_id is not None and fence is not None:
            await current_attempt(session, job, attempt_id, fence)
        event = TrainingEvent(
            id=job.next_event_sequence,
            job_id=job_id,
            attempt_id=attempt_id,
            state_version=job.version,
            occurred_at=datetime.now(UTC),
            kind=payload.kind,
            payload=payload,
        )
        row = TrainingEventRow(
            job_id=job.id,
            sequence=event.id,
            attempt_id=attempt_id,
            fence=fence,
            state_version=job.version,
            payload=payload.model_dump(mode="json"),
            occurred_at=event.occurred_at,
        )
        job.next_event_sequence += 1
        session.add(row)
        await session.flush()
        event_count.add(1, {"kind": payload.kind})
        logger.info(
            "training event recorded",
            extra={
                "job_id": job_id,
                "attempt_id": attempt_id,
                "event_kind": payload.kind,
                "sequence": event.id,
            },
        )
        return event


async def record_metric(
    session: AsyncSession, sample: TrainingMetricSample, *, fence: int
) -> TrainingMetricRow:
    with tracer.start_as_current_span("coire.api.training.metric.record"):
        job = await current_job(session, sample.job_id, lock=True)
        await current_attempt(session, job, sample.attempt_id, fence)
        existing = await session.scalar(
            select(TrainingMetricRow).where(
                TrainingMetricRow.job_id == sample.job_id,
                TrainingMetricRow.attempt_id == sample.attempt_id,
                TrainingMetricRow.completed_update == sample.update,
                TrainingMetricRow.kind == sample.kind,
            )
        )
        payload = sample.model_dump(mode="json")
        if existing is not None:
            if existing.metric != payload:
                raise TrainingConflict("Recorded training metric is immutable")
            return existing
        row = TrainingMetricRow(
            id=uuid.uuid4(),
            job_id=sample.job_id,
            attempt_id=sample.attempt_id,
            completed_update=sample.update,
            kind=sample.kind,
            loss=sample.loss,
            metric=payload,
            rolled_back=False,
            recorded_at=sample.recorded_at,
        )
        if sample.kind == "train":
            job.completed_update = max(job.completed_update, sample.update)
        session.add(row)
        await session.flush()
        await append_event(
            session,
            job.id,
            TrainingProgressEvent(metric=sample),
            attempt_id=sample.attempt_id,
            fence=fence,
        )
        metric_count.add(1, {"kind": sample.kind})
        return row


async def replay_events(
    session: AsyncSession, job_id: str, *, after: int = 0, limit: int = 100
) -> TrainingReplayPage:
    if after < 0 or not 1 <= limit <= 100:
        raise TrainingValidationError("Invalid event cursor or page limit")
    with tracer.start_as_current_span("coire.api.training.event.replay"):
        job = await current_job(session, job_id)
        head = job.next_event_sequence - 1
        if after > head:
            raise TrainingConflict("Event cursor is newer than this job's persisted history")
        earliest = await session.scalar(
            select(func.min(TrainingEventRow.sequence)).where(TrainingEventRow.job_id == job_id)
        )
        if head and (earliest is None or after < earliest - 1):
            snapshot = TrainingJobSnapshot.model_validate(
                {
                    "id": job.id,
                    "version": job.version,
                    "state": job.state,
                    "completed_update": job.completed_update,
                    "latest_checkpoint_id": job.latest_checkpoint_id,
                    "adapter_id": job.adapter_id,
                    "reason": job.safe_reason,
                }
            )
            return TrainingReplayPage(events=[], cursor=head, reset=snapshot)
        rows = list(
            (
                await session.scalars(
                    select(TrainingEventRow)
                    .where(TrainingEventRow.job_id == job_id, TrainingEventRow.sequence > after)
                    .order_by(TrainingEventRow.sequence)
                    .limit(limit)
                )
            ).all()
        )
        events = [project_event(row) for row in rows]
        return TrainingReplayPage(events=events, cursor=events[-1].id if events else after)


async def metric_page(
    session: AsyncSession, job_id: str, *, cursor: str | None = None, limit: int = 2000
) -> TrainingMetricPage:
    if not 1 <= limit <= 2000:
        raise TrainingValidationError("Invalid metric page limit")
    await current_job(session, job_id)
    from coire_api.training.service import decode_page_cursor, encode_page_cursor

    scope = "metrics:" + job_id
    statement = select(TrainingMetricRow).where(TrainingMetricRow.job_id == job_id)
    if cursor is not None:
        try:
            if len(cursor) > 512:
                raise ValueError("oversized cursor")
            recorded_at, identity = decode_page_cursor(cursor, scope)
            position = TrainingMetricCursor.model_validate(
                {"job_id": job_id, "recorded_at": recorded_at, "id": identity}
            )
        except (ValueError, binascii.Error, ValidationError) as error:
            raise TrainingValidationError("Invalid metric cursor") from error
        statement = statement.where(
            or_(
                TrainingMetricRow.recorded_at > position.recorded_at,
                and_(
                    TrainingMetricRow.recorded_at == position.recorded_at,
                    TrainingMetricRow.id > position.id,
                ),
            )
        )
    rows = list(
        (
            await session.scalars(
                statement.order_by(TrainingMetricRow.recorded_at, TrainingMetricRow.id).limit(
                    limit + 1
                )
            )
        ).all()
    )
    items = [
        TrainingMetricSample.model_validate({**row.metric, "rolled_back": row.rolled_back})
        for row in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_page_cursor(scope, last.recorded_at, str(last.id))
    return TrainingMetricPage(items=items, next_cursor=next_cursor)
