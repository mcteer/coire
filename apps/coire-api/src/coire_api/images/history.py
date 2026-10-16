"""Permanent owner history deletion, fenced from active generation and output storage."""

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.auth import Principal, audit_actor
from coire_api.db import ImageJobEventRow, ImageJobRow
from coire_api.images.authorization import authorize_live_image_action
from coire_core.errors import ImageConflict, ImageNotFound
from coire_core.models.images import ImageHistoryDeletionReceipt, ImageHistoryDeletionRequest


async def delete_owned_history(
    session: AsyncSession, principal: Principal, body: ImageHistoryDeletionRequest
) -> ImageHistoryDeletionReceipt:
    owner = await authorize_live_image_action(session, principal)
    # The owner row lock serializes this mutation with admission and other owner actions.
    rows = (
        await session.scalars(
            select(ImageJobRow)
            .where(ImageJobRow.id.in_(body.job_ids), ImageJobRow.owner_user_id == owner)
            .order_by(ImageJobRow.id)
            .with_for_update()
        )
    ).all()
    if len(rows) != len(body.job_ids):
        raise ImageNotFound()
    if any(row.state not in {"succeeded", "failed", "cancelled"} for row in rows):
        raise ImageConflict("Only completed generation history can be deleted")
    now = datetime.now(UTC)
    await session.execute(delete(ImageJobEventRow).where(ImageJobEventRow.job_id.in_(body.job_ids)))
    for row in rows:
        row.history_deleted_at = row.history_deleted_at or now
    actor, actor_type, actor_user_id = audit_actor(principal)
    await write_audit(
        session,
        actor=actor,
        actor_type=actor_type,
        action="image.history.deleted",
        target_type="image_history",
        target_id=str(owner),
        detail={"job_ids": body.job_ids},
        actor_user_id=actor_user_id,
    )
    return ImageHistoryDeletionReceipt(deleted_job_ids=body.job_ids)
