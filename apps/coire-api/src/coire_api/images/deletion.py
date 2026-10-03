"""Owner tombstones and no-follow physical purge for private image output blobs."""

from __future__ import annotations

import os
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import ImageOutputRow, ImageQuotaRow
from coire_api.images.downloads import _safe_blob_parts
from coire_core.errors import ImageNotFound, ImageStorageUnavailable
from coire_core.models.images import ImageDeletionReceipt


async def tombstone_owned_output(
    session: AsyncSession, principal: Principal, output_id: uuid.UUID
) -> ImageDeletionReceipt:
    """Lock the output; newly authorized reads fail once the caller commits."""
    row = await session.get(ImageOutputRow, output_id, populate_existing=True, with_for_update=True)
    if row is None or row.owner_user_id != principal.user_id or row.state != "published":
        raise ImageNotFound()
    if row.purged_at is not None:
        return ImageDeletionReceipt(output_id=output_id, state="purged")
    if row.deleted_at is None:
        row.deleted_at = datetime.now(UTC)
    return ImageDeletionReceipt(output_id=output_id, state="tombstoned")


def _unlink_blob(root: Path, key: str) -> None:
    """Remove only a regular file below the API-owned root; absence is retry-safe."""
    parts = _safe_blob_parts(key)
    parent_fd = -1
    try:
        parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        try:
            details = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ImageStorageUnavailable()
        os.unlink(parts[-1], dir_fd=parent_fd)
        os.fsync(parent_fd)
    except (OSError, ImageStorageUnavailable) as exc:
        raise ImageStorageUnavailable() from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def purge_output_blob(
    root: Path, row: ImageOutputRow, owner_quota: ImageQuotaRow, global_quota: ImageQuotaRow
) -> None:
    """Release storage accounting only after the blob is physically absent."""
    if row.purged_at is not None:
        return
    if (
        row.state != "published"
        or row.deleted_at is None
        or owner_quota.owner_user_id != row.owner_user_id
        or owner_quota.scope != "owner"
        or global_quota.scope != "global"
        or owner_quota.stored_bytes < row.size_bytes
        or global_quota.stored_bytes < row.size_bytes
    ):
        raise ImageStorageUnavailable()
    _unlink_blob(root, row.blob_key)
    owner_quota.stored_bytes -= row.size_bytes
    global_quota.stored_bytes -= row.size_bytes
    row.purged_at = datetime.now(UTC)
