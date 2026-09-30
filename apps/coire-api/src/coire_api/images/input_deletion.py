"""Owner-scoped tombstones for private image inputs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import ImageInputRow
from coire_api.images.inputs import project_image_input
from coire_core.errors import ImageConflict, ImageNotFound
from coire_core.models.images import ImageInput


async def tombstone_owned_input(
    session: AsyncSession, principal: Principal, input_id: uuid.UUID
) -> ImageInput:
    """Commit this row with the request so later status/use treats it as absent."""
    row = await session.get(ImageInputRow, input_id, populate_existing=True, with_for_update=True)
    if row is None or row.owner_user_id != principal.user_id:
        raise ImageNotFound()
    if row.deleted_at is not None:
        return project_image_input(row)
    if row.active_references:
        raise ImageConflict("image input is referenced by an active job")
    now = datetime.now(UTC)
    row.deleted_at = now
    row.updated_at = now
    row.state = "purged" if row.purged_at is not None else "deleting"
    return project_image_input(row)
