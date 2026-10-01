"""Owner-scoped tombstones for private image inputs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.auth import Principal, audit_actor
from coire_api.db import ImageInputRow, ImageJobRow
from coire_api.images.cancellation import request_image_job_cancel
from coire_api.images.inputs import project_image_input
from coire_api.images.quota import _QUOTA_LOCK
from coire_core.errors import ImageConflict, ImageNotFound
from coire_core.models.audit import AuditOutcome
from coire_core.models.images import ImageInput


async def tombstone_owned_input(
    session: AsyncSession, principal: Principal, input_id: uuid.UUID
) -> ImageInput:
    """Commit this row with the request so later status/use treats it as absent."""
    await session.execute(_QUOTA_LOCK)
    row = await session.get(ImageInputRow, input_id, populate_existing=True, with_for_update=True)
    if row is None or row.owner_user_id != principal.user_id:
        raise ImageNotFound()
    if row.deleted_at is not None:
        return project_image_input(row)
    requested_jobs = 0
    if row.active_references:
        wanted = str(input_id)
        jobs = (
            await session.scalars(
                select(ImageJobRow.id)
                .where(
                    ImageJobRow.owner_user_id == row.owner_user_id,
                    ImageJobRow.state.in_(("queued", "reserving", "running", "cancelling")),
                    or_(
                        ImageJobRow.resolved_spec["effective_spec"]["init_image_id"].astext
                        == wanted,
                        ImageJobRow.resolved_spec["effective_spec"]["mask_id"].astext == wanted,
                        ImageJobRow.resolved_spec["effective_spec"]["control"]["image_id"].astext
                        == wanted,
                    ),
                )
                .order_by(ImageJobRow.id)
                .limit(101)
            )
        ).all()
        if not jobs or len(jobs) > 100:
            raise ImageConflict("image input references cannot be reconciled")
        requested_jobs = len(jobs)
        for job_id in jobs:
            await request_image_job_cancel(session, principal, job_id, commit=False)
    now = datetime.now(UTC)
    row.deleted_at = now
    row.updated_at = now
    row.state = "purged" if row.purged_at is not None else "deleting"
    actor, actor_type, actor_user_id = audit_actor(principal)
    await write_audit(
        session,
        actor=actor,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action="image.input.delete",
        target_type="image_input",
        target_id=str(input_id),
        outcome=AuditOutcome.OK,
        context={"active_jobs_cancel_requested": requested_jobs},
    )
    return project_image_input(row)
