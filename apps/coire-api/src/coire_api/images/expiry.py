"""Expire never-dispatched image jobs without releasing uncertain node work."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import ImageExecutionLeaseRow, ImageJobEventRow, ImageJobRow
from coire_api.images.job_capacity import release_pending_image_job_capacity
from coire_api.images.jobs import _policy
from coire_api.images.quota import _QUOTA_LOCK
from coire_core.errors import ImageConflict
from coire_core.models.audit import AuditOutcome
from coire_core.models.images import ImageJobEvent, ImageJobState


async def expire_queued_image_job(
    session: AsyncSession, job_id: str, *, now: datetime | None = None
) -> bool:
    """Fail an overdue, unplaced job and release its queue and byte reservations atomically."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ImageConflict("image expiry time must be aware")
    await session.execute(_QUOTA_LOCK)
    row = await session.get(ImageJobRow, job_id, populate_existing=True, with_for_update=True)
    if row is None or row.state != ImageJobState.QUEUED:
        return False
    if row.deadline_at.tzinfo is None or row.deadline_at > current:
        return False
    # Placement may have begun while state remains queued. Its lease and node scratch must
    # be reconciled before any hold is released.
    if (
        row.fence != 0
        or row.selected_node_id is not None
        or row.instance_id is not None
        or row.reservation_ids
        or row.cancel_requested_at is not None
    ):
        raise ImageConflict("image queue expiry has uncertain placement")
    if (
        await session.scalar(
            select(ImageExecutionLeaseRow.id)
            .where(
                ImageExecutionLeaseRow.job_id == job_id,
                ImageExecutionLeaseRow.released_at.is_(None),
            )
            .limit(1)
        )
        is not None
    ):
        raise ImageConflict("image queue expiry has active execution lease")
    snapshot, _, _ = _policy(row)
    if snapshot.resolved is not None:
        raise ImageConflict("image queue expiry has bound runtime")
    held = row.authorization_snapshot.get("output_hold_bytes")
    if not isinstance(held, int) or isinstance(held, bool) or held <= 0:
        raise ImageConflict("image capacity hold unavailable")
    latest = await session.scalar(
        select(func.max(ImageJobEventRow.sequence)).where(ImageJobEventRow.job_id == job_id)
    )
    if latest is None or latest < 1:
        raise ImageConflict("image event history unavailable")
    await release_pending_image_job_capacity(
        session, row.owner_user_id, snapshot.effective_spec.n, held
    )
    row.state = ImageJobState.FAILED
    row.safe_failure_code = "queue_timeout"
    row.cleanup_state = "cleaned"
    row.receipt_state = "none"
    row.updated_at = current
    row.finished_at = current
    row.version += 1
    event = ImageJobEvent(
        job_id=job_id,
        sequence=latest + 1,
        at=current,
        type="error",
        state=ImageJobState.FAILED,
        safe_code="queue_timeout",
    )
    session.add(
        ImageJobEventRow(
            job_id=job_id,
            sequence=event.sequence,
            event_type=event.type,
            payload=event.model_dump(mode="json"),
            created_at=current,
        )
    )
    await write_audit(
        session,
        actor="coire-scheduler",
        action="image.queue.expired",
        target_type="image_job",
        target_id=job_id,
        outcome=AuditOutcome.OK,
        context={"reason": "queue_timeout"},
    )
    return True
