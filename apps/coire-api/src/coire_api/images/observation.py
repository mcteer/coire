"""Apply a fenced Studio journal observation without restarting image generation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageExecutionLeaseRow, ImageJobEventRow, ImageJobRow
from coire_core.errors import ImageConflict
from coire_core.models.image_worker import NodeImageJob
from coire_core.models.images import ImageJobEvent, ImageJobState

_PROGRESS_INTERVAL = timedelta(milliseconds=250)


async def reconcile_image_observation(
    session: AsyncSession,
    job_id: str,
    selected_node_id: uuid.UUID,
    observed: NodeImageJob,
    *,
    expected_node: str,
) -> bool:
    """Persist only monotonic progress from the bound attempt and active lease.

    Return true while the generation journal still needs observation. A false
    result means another workflow can handle transfer or cancellation.
    """
    row = await session.get(ImageJobRow, job_id, populate_existing=True, with_for_update=True)
    if row is None or row.state not in {"reserving", "running"}:
        return False
    if row.cancel_requested_at is not None:
        return False
    if (
        observed.job_id != row.id
        or observed.attempt != row.attempt
        or observed.fence != row.fence
        or observed.node != expected_node
        or observed.instance_id != row.instance_id
        or row.selected_node_id != selected_node_id
        or row.fence < 1
    ):
        raise ImageConflict("node image attempt differs from core")
    lease = await session.scalar(
        select(ImageExecutionLeaseRow).where(
            ImageExecutionLeaseRow.job_id == job_id,
            ImageExecutionLeaseRow.node_id == selected_node_id,
            ImageExecutionLeaseRow.fence == row.fence,
            ImageExecutionLeaseRow.mode == "image",
            ImageExecutionLeaseRow.released_at.is_(None),
        )
    )
    if lease is None:
        raise ImageConflict("image execution lease is unavailable")
    if observed.state in {"failed", "cancelled", "cancelling"}:
        raise ImageConflict("node image attempt needs failure recovery")
    if observed.state in {"transferring", "succeeded"}:
        row.state = ImageJobState.TRANSFERRING
        row.progress = max(row.progress, 0.99)
        row.updated_at = datetime.now(UTC)
        row.version += 1
        return False
    if observed.state in {"queued", "reserving"}:
        return True
    if observed.state != "running":
        raise ImageConflict("node image state is unsupported")

    latest = await session.scalar(
        select(ImageJobEventRow)
        .where(ImageJobEventRow.job_id == job_id)
        .order_by(ImageJobEventRow.sequence.desc())
        .limit(1)
    )
    if latest is None:
        raise ImageConflict("image event history unavailable")
    now = datetime.now(UTC)
    starting = row.state == "reserving"
    changed = starting
    if starting:
        row.state = ImageJobState.RUNNING
    step = observed.progress_step
    total = observed.progress_total
    progress = step / total if step is not None and total is not None else None
    if progress is not None and not 0 <= progress <= 1:
        raise ImageConflict("node image progress is invalid")
    if progress is not None and min(progress, 0.99) > row.progress:
        row.progress = min(progress, 0.99)
        changed = True
    if changed:
        row.updated_at = now
        row.version += 1
    if starting:
        event = ImageJobEvent(
            job_id=job_id,
            sequence=latest.sequence + 1,
            at=now,
            type="started",
            state=ImageJobState.RUNNING,
            cache_status=observed.cache_status,
            worker_residency="resident",
        )
    elif (
        progress is not None
        and progress > 0
        and latest.event_type in {"started", "progress"}
        and now - latest.created_at >= _PROGRESS_INTERVAL
        and changed
    ):
        event = ImageJobEvent(
            job_id=job_id,
            sequence=latest.sequence + 1,
            at=now,
            type="progress",
            state=ImageJobState.RUNNING,
            stage="generation",
            step=step,
            total_steps=total,
            cache_status=observed.cache_status,
            worker_residency="resident",
        )
    else:
        return True
    session.add(
        ImageJobEventRow(
            job_id=job_id,
            sequence=event.sequence,
            event_type=event.type,
            payload=event.model_dump(mode="json"),
            created_at=now,
        )
    )
    return True
