"""Atomic queue, daily-output and disk reservation for image jobs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.images.quota import _free_bytes, _locked_rows
from coire_core.errors import ImageConflict, ImageQuotaExceeded, ImageStorageUnavailable
from coire_core.settings import Settings


async def reserve_image_job_capacity(
    session: AsyncSession,
    owner_id: uuid.UUID,
    outputs: int,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> int:
    """Caller commits this hold with the queued job and its audit row."""
    if not 1 <= outputs <= settings.image_max_outputs:
        raise ImageConflict("invalid image output count")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ImageConflict("image quota time must be aware")
    today = current.astimezone(UTC).date()
    global_row, owner_row = await _locked_rows(session, owner_id)
    # A midnight rollover does not free slots already held by jobs from yesterday.
    owner_consumed = owner_row.consumed_outputs if owner_row.day_bucket == today else 0
    if (
        owner_row.pending_jobs >= settings.image_pending_per_owner
        or global_row.pending_jobs >= settings.image_pending_global
        or owner_consumed + owner_row.held_outputs + outputs
        > settings.image_daily_outputs_per_owner
    ):
        raise ImageQuotaExceeded()
    amount = outputs * settings.image_output_max_bytes
    if (
        owner_row.held_bytes + owner_row.stored_bytes + amount
        > settings.image_owner_storage_quota_bytes
        or global_row.held_bytes + global_row.stored_bytes + amount
        > settings.image_global_storage_quota_bytes
    ):
        raise ImageQuotaExceeded()
    if (
        _free_bytes(Path(settings.image_blob_root))
        < global_row.held_bytes + amount + settings.image_disk_safety_floor_bytes
    ):
        raise ImageStorageUnavailable()
    if owner_row.day_bucket != today:
        owner_row.day_bucket = today
        owner_row.consumed_outputs = 0
    if global_row.day_bucket != today:
        global_row.day_bucket = today
        global_row.consumed_outputs = 0
    owner_row.pending_jobs += 1
    global_row.pending_jobs += 1
    owner_row.held_outputs += outputs
    global_row.held_outputs += outputs
    owner_row.held_bytes += amount
    global_row.held_bytes += amount
    return amount


async def mark_image_job_started(
    session: AsyncSession,
    owner_id: uuid.UUID,
    outputs: int,
    *,
    now: datetime | None = None,
) -> None:
    """Move one locked queued job to started, consuming its output allowance."""
    if outputs <= 0:
        raise ImageConflict("invalid image output count")
    global_row, owner_row = await _locked_rows(session, owner_id)
    if (
        owner_row.pending_jobs < 1
        or global_row.pending_jobs < 1
        or owner_row.held_outputs < outputs
        or global_row.held_outputs < outputs
    ):
        raise ImageConflict("image queue reservation missing")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ImageConflict("image quota time must be aware")
    today = current.astimezone(UTC).date()
    if owner_row.day_bucket != today:
        owner_row.day_bucket = today
        owner_row.consumed_outputs = 0
    if global_row.day_bucket != today:
        global_row.day_bucket = today
        global_row.consumed_outputs = 0
    owner_row.pending_jobs -= 1
    global_row.pending_jobs -= 1
    owner_row.held_outputs -= outputs
    global_row.held_outputs -= outputs
    owner_row.consumed_outputs += outputs
    global_row.consumed_outputs += outputs


async def release_pending_image_job_capacity(
    session: AsyncSession, owner_id: uuid.UUID, outputs: int, held_bytes: int
) -> None:
    """Release a queued job before it ran; caller fences the job row first."""
    if outputs <= 0 or held_bytes <= 0:
        raise ImageConflict("invalid image queue release")
    global_row, owner_row = await _locked_rows(session, owner_id)
    if (
        owner_row.pending_jobs < 1
        or global_row.pending_jobs < 1
        or owner_row.held_outputs < outputs
        or global_row.held_outputs < outputs
        or owner_row.held_bytes < held_bytes
        or global_row.held_bytes < held_bytes
    ):
        raise ImageConflict("image queue reservation missing")
    owner_row.pending_jobs -= 1
    global_row.pending_jobs -= 1
    owner_row.held_outputs -= outputs
    global_row.held_outputs -= outputs
    owner_row.held_bytes -= held_bytes
    global_row.held_bytes -= held_bytes
