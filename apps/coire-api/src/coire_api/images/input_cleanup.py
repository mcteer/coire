"""No-follow cleanup for failed and orphaned private image input originals."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from opentelemetry import metrics, trace
from sqlalchemy import func, or_, select

from coire_api.db import ImageInputRow, ImageQuotaRow, session_scope
from coire_api.images.quota import _QUOTA_LOCK, _locked_rows
from coire_core.errors import ImageStorageUnavailable
from coire_core.settings import Settings

_BATCH_SIZE = 25
_MAX_DIRECTORY_ENTRIES = 4096
_ORPHAN_GRACE_SECONDS = 3600
tracer = trace.get_tracer("coire.api.image")
logger = logging.getLogger(__name__)
cleanup_total = metrics.get_meter("coire.api.image").create_counter(
    "coire_image_input_cleanup_total", unit="1", description="Private image input cleanup outcomes"
)
input_purge_oldest_seconds = metrics.get_meter("coire.api.image").create_gauge(
    "coire_image_input_purge_oldest_seconds",
    unit="s",
    description="Age of oldest failed or deleted image input awaiting physical cleanup",
)


def _canonical_uuid(value: str) -> uuid.UUID | None:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value else None


def _input_id_for_name(name: str) -> uuid.UUID | None:
    direct = _canonical_uuid(name)
    if direct is not None:
        return direct
    if not (name.startswith(".") and name.endswith(".uploading")):
        return None
    parts = name[1 : -len(".uploading")].split(".")
    if len(parts) != 2 or _canonical_uuid(parts[1]) is None:
        return None
    return _canonical_uuid(parts[0])


def _unlink_generated_file(root: Path, name: str) -> None:
    if _input_id_for_name(name) is None:
        raise ImageStorageUnavailable()
    root_fd = -1
    try:
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        try:
            details = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ImageStorageUnavailable()
        os.unlink(name, dir_fd=root_fd)
        os.fsync(root_fd)
    except (OSError, ImageStorageUnavailable) as exc:
        raise ImageStorageUnavailable() from exc
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def purge_failed_input_bytes(
    root: Path,
    row: ImageInputRow,
    owner_quota: ImageQuotaRow,
    global_quota: ImageQuotaRow,
    derived_root: Path | None = None,
) -> None:
    """Retain failed status while releasing its hold only after unlink/absence."""
    if row.purged_at is not None:
        return
    held = row.held_bytes
    if (
        row.state != "failed"
        or row.purpose not in {"recipe", "init", "mask", "control"}
        or row.original_key != str(row.id)
        or held <= 0
        or owner_quota.scope != "owner"
        or owner_quota.owner_user_id != row.owner_user_id
        or global_quota.scope != "global"
        or owner_quota.held_bytes < held
        or global_quota.held_bytes < held
    ):
        raise ImageStorageUnavailable()
    if row.purpose != "recipe":
        if derived_root is None:
            raise ImageStorageUnavailable()
        _unlink_generated_file(derived_root, str(row.id))
    _unlink_generated_file(root, row.original_key)
    owner_quota.held_bytes -= held
    global_quota.held_bytes -= held
    row.held_bytes = 0
    row.purged_at = datetime.now(UTC)


async def purge_failed_input(settings: Settings, input_id: uuid.UUID) -> bool:
    async with session_scope() as session:
        snapshot = await session.get(ImageInputRow, input_id)
        if snapshot is None or snapshot.state != "failed" or snapshot.purged_at is not None:
            return False
        global_quota, owner_quota = await _locked_rows(session, snapshot.owner_user_id)
        row = await session.get(
            ImageInputRow, input_id, populate_existing=True, with_for_update=True
        )
        if row is None or row.state != "failed" or row.purged_at is not None:
            return False
        with tracer.start_as_current_span("coire.api.image.input_purge"):
            await asyncio.to_thread(
                purge_failed_input_bytes,
                Path(settings.image_input_original_root),
                row,
                owner_quota,
                global_quota,
                Path(settings.image_input_derived_root),
            )
        return True


def purge_deleted_input_bytes(
    root: Path,
    row: ImageInputRow,
    owner_quota: ImageQuotaRow,
    global_quota: ImageQuotaRow,
    derived_root: Path | None = None,
) -> None:
    """Release original and normalized storage only after both are absent."""
    if row.purged_at is not None:
        return
    held = row.held_bytes
    stored = 0 if held > 0 else row.original_bytes + (row.normalized_bytes or 0)
    if (
        row.state != "deleting"
        or row.deleted_at is None
        or row.purpose not in {"recipe", "init", "mask", "control"}
        or row.original_key != str(row.id)
        or row.active_references != 0
        or row.original_bytes <= 0
        or held < 0
        or stored < 0
        or owner_quota.scope != "owner"
        or owner_quota.owner_user_id != row.owner_user_id
        or global_quota.scope != "global"
        or owner_quota.held_bytes < held
        or global_quota.held_bytes < held
        or owner_quota.stored_bytes < stored
        or global_quota.stored_bytes < stored
    ):
        raise ImageStorageUnavailable()
    if row.purpose != "recipe":
        if derived_root is None:
            raise ImageStorageUnavailable()
        _unlink_generated_file(derived_root, str(row.id))
    _unlink_generated_file(root, row.original_key)
    owner_quota.held_bytes -= held
    global_quota.held_bytes -= held
    owner_quota.stored_bytes -= stored
    global_quota.stored_bytes -= stored
    row.held_bytes = 0
    row.state = "purged"
    row.purged_at = datetime.now(UTC)
    row.updated_at = row.purged_at


async def purge_deleted_input(settings: Settings, input_id: uuid.UUID) -> bool:
    async with session_scope() as session:
        snapshot = await session.get(ImageInputRow, input_id)
        if snapshot is None or snapshot.state != "deleting" or snapshot.purged_at is not None:
            return False
        if (
            snapshot.purpose != "recipe"
            and snapshot.held_bytes > 0
            and snapshot.deleted_at is not None
            and snapshot.deleted_at > datetime.now(UTC) - timedelta(hours=1)
        ):
            # A worker may still own the original descriptor after a tombstone.
            return False
        global_quota, owner_quota = await _locked_rows(session, snapshot.owner_user_id)
        row = await session.get(
            ImageInputRow, input_id, populate_existing=True, with_for_update=True
        )
        if row is None or row.state != "deleting" or row.purged_at is not None:
            return False
        with tracer.start_as_current_span("coire.api.image.input_purge"):
            await asyncio.to_thread(
                purge_deleted_input_bytes,
                Path(settings.image_input_original_root),
                row,
                owner_quota,
                global_quota,
                Path(settings.image_input_derived_root),
            )
        return True


async def sweep_deleted_inputs(settings: Settings) -> int:
    async with session_scope() as session:
        pending = (
            await session.scalars(
                select(ImageInputRow.id)
                .where(ImageInputRow.state == "deleting", ImageInputRow.purged_at.is_(None))
                .order_by(ImageInputRow.deleted_at, ImageInputRow.id)
                .limit(_BATCH_SIZE)
            )
        ).all()
    purged = 0
    for input_id in pending:
        try:
            if await purge_deleted_input(settings, input_id):
                cleanup_total.add(1, {"kind": "deleted", "outcome": "succeeded"})
                purged += 1
        except Exception as exc:
            cleanup_total.add(1, {"kind": "deleted", "outcome": "failed"})
            logger.error(
                "image input purge failed input_id=%s error_type=%s",
                input_id,
                type(exc).__name__,
            )
    return purged


async def sweep_failed_inputs(settings: Settings) -> int:
    async with session_scope() as session:
        pending = (
            await session.scalars(
                select(ImageInputRow.id)
                .where(
                    ImageInputRow.state == "failed",
                    ImageInputRow.purged_at.is_(None),
                    ImageInputRow.held_bytes > 0,
                )
                .order_by(ImageInputRow.created_at, ImageInputRow.id)
                .limit(_BATCH_SIZE)
            )
        ).all()
        oldest = await session.scalar(
            select(func.min(ImageInputRow.updated_at)).where(
                or_(ImageInputRow.state == "failed", ImageInputRow.state == "deleting"),
                ImageInputRow.purged_at.is_(None),
            )
        )
    age = max(0.0, (datetime.now(UTC) - oldest).total_seconds()) if oldest else 0.0
    input_purge_oldest_seconds.set(age)
    purged = 0
    for input_id in pending:
        try:
            if await purge_failed_input(settings, input_id):
                cleanup_total.add(1, {"kind": "failed", "outcome": "succeeded"})
                purged += 1
        except Exception as exc:
            cleanup_total.add(1, {"kind": "failed", "outcome": "failed"})
            logger.error(
                "image input purge failed input_id=%s error_type=%s",
                input_id,
                type(exc).__name__,
            )
    return purged


def _old_generated_names(root: Path) -> list[tuple[str, uuid.UUID]]:
    cutoff = datetime.now(UTC).timestamp() - _ORPHAN_GRACE_SECONDS
    root_fd = -1
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ImageStorageUnavailable() from exc
    try:
        found: list[tuple[str, uuid.UUID]] = []
        with os.scandir(root_fd) as entries:
            for inspected, entry in enumerate(entries, start=1):
                if inspected > _MAX_DIRECTORY_ENTRIES:
                    raise ImageStorageUnavailable()
                input_id = _input_id_for_name(entry.name)
                if input_id is None:
                    continue
                details = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(details.st_mode) and details.st_mtime < cutoff:
                    found.append((entry.name, input_id))
                    if len(found) >= _BATCH_SIZE:
                        break
        return found
    except OSError as exc:
        raise ImageStorageUnavailable() from exc
    finally:
        os.close(root_fd)


async def sweep_orphan_inputs(settings: Settings) -> int:
    roots = (
        ("original", Path(settings.image_input_original_root)),
        ("derived", Path(settings.image_input_derived_root)),
    )
    removed = 0
    for kind, root in roots:
        candidates = await asyncio.to_thread(_old_generated_names, root)
        for name, input_id in candidates:
            try:
                async with session_scope() as session:
                    # Admission holds this lock through its commit, so row absence is final.
                    await session.execute(_QUOTA_LOCK)
                    row = await session.get(ImageInputRow, input_id)
                    if row is not None and (kind == "original" or row.purged_at is None):
                        continue
                    await asyncio.to_thread(_unlink_generated_file, root, name)
                cleanup_total.add(1, {"kind": "orphan", "outcome": "succeeded"})
                removed += 1
            except Exception as exc:
                cleanup_total.add(1, {"kind": "orphan", "outcome": "failed"})
                logger.error(
                    "image input orphan cleanup failed input_id=%s error_type=%s",
                    input_id,
                    type(exc).__name__,
                )
    return removed
