"""Transaction-bound owner/global storage holds for private image assets."""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageQuotaRow
from coire_core.errors import ImageConflict, ImageQuotaExceeded, ImageStorageUnavailable
from coire_core.settings import Settings

# One short global transaction lock covers first creation of both partial-unique rows.
# Admission throughput is bounded to 32 pending jobs, so serial quota mutations are deliberate.
_QUOTA_LOCK = text("SELECT pg_advisory_xact_lock(2637076765346304)")


def _free_bytes(root: Path) -> int:
    try:
        return shutil.disk_usage(root).free
    except OSError as exc:
        raise ImageStorageUnavailable() from exc


async def _locked_rows(
    session: AsyncSession, owner_id: uuid.UUID
) -> tuple[ImageQuotaRow, ImageQuotaRow]:
    await session.execute(_QUOTA_LOCK)
    global_row = await session.scalar(
        select(ImageQuotaRow).where(ImageQuotaRow.scope == "global").with_for_update()
    )
    if global_row is None:
        global_row = ImageQuotaRow(
            id=uuid.uuid4(),
            scope="global",
            owner_user_id=None,
            held_bytes=0,
            stored_bytes=0,
            pending_jobs=0,
            day_bucket=datetime.now(UTC).date(),
            held_outputs=0,
            consumed_outputs=0,
        )
        session.add(global_row)
        await session.flush()
    owner_row = await session.scalar(
        select(ImageQuotaRow)
        .where(ImageQuotaRow.scope == "owner", ImageQuotaRow.owner_user_id == owner_id)
        .with_for_update()
    )
    if owner_row is None:
        owner_row = ImageQuotaRow(
            id=uuid.uuid4(),
            scope="owner",
            owner_user_id=owner_id,
            held_bytes=0,
            stored_bytes=0,
            pending_jobs=0,
            day_bucket=datetime.now(UTC).date(),
            held_outputs=0,
            consumed_outputs=0,
        )
        session.add(owner_row)
        await session.flush()
    if (
        global_row.scope != "global"
        or global_row.owner_user_id is not None
        or owner_row.scope != "owner"
        or owner_row.owner_user_id != owner_id
    ):
        raise ImageConflict("image quota identity mismatch")
    return global_row, owner_row


async def reserve_storage_hold(
    session: AsyncSession, owner_id: uuid.UUID, amount: int, settings: Settings
) -> None:
    """Reserve actual or worst-case bytes; commit with the owning input/job row."""
    if amount <= 0:
        raise ImageConflict("image storage hold must be positive")
    global_row, owner_row = await _locked_rows(session, owner_id)
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
    owner_row.held_bytes += amount
    global_row.held_bytes += amount


async def settle_storage_hold(
    session: AsyncSession, owner_id: uuid.UUID, held: int, stored: int
) -> None:
    """Move a specific hold to stored usage after durable bytes are published."""
    if held <= 0 or stored < 0 or stored > held:
        raise ImageConflict("invalid image storage settlement")
    global_row, owner_row = await _locked_rows(session, owner_id)
    if global_row.held_bytes < held or owner_row.held_bytes < held:
        raise ImageConflict("image storage hold missing")
    global_row.held_bytes -= held
    owner_row.held_bytes -= held
    global_row.stored_bytes += stored
    owner_row.stored_bytes += stored


async def release_storage_hold(session: AsyncSession, owner_id: uuid.UUID, held: int) -> None:
    """Release a failed/cancelled hold once, without crediting stored usage."""
    if held <= 0:
        raise ImageConflict("invalid image storage hold")
    global_row, owner_row = await _locked_rows(session, owner_id)
    if global_row.held_bytes < held or owner_row.held_bytes < held:
        raise ImageConflict("image storage hold missing")
    global_row.held_bytes -= held
    owner_row.held_bytes -= held
