"""Release owner input holds in the same transaction as a terminal image job."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageInputRow, ImageJobRow
from coire_core.errors import ImageConflict
from coire_core.models.images import ImageJobSettingsSnapshot


async def release_image_input_references(session: AsyncSession, job: ImageJobRow) -> None:
    """A terminal transition releases each bound input once under row locks."""
    try:
        snapshot = ImageJobSettingsSnapshot.model_validate(job.resolved_spec)
    except ValueError as exc:
        raise ImageConflict("image input binding is unavailable") from exc
    spec = snapshot.effective_spec
    ids = {item for item in (spec.init_image_id, spec.mask_id) if item is not None}
    if spec.control is not None:
        ids.add(spec.control.image_id)
    for input_id in sorted(ids):
        row = await session.get(
            ImageInputRow, input_id, populate_existing=True, with_for_update=True
        )
        if row is None or row.owner_user_id != job.owner_user_id or row.active_references < 1:
            raise ImageConflict("image input reference is unavailable")
        row.active_references -= 1
