"""Transactional image cancellation intent; uncertain node work keeps its holds."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.auth import Principal, PrincipalKind, audit_actor
from coire_api.db import ImageJobEventRow, ImageJobRow
from coire_api.images.authorization import authorize_live_image_action
from coire_api.images.job_capacity import release_pending_image_job_capacity
from coire_api.images.jobs import _policy, get_owned_image_job
from coire_api.images.quota import _QUOTA_LOCK
from coire_core.errors import ImageConflict, ImageForbidden, ImageNotFound
from coire_core.models.audit import AuditOutcome
from coire_core.models.images import ImageJob, ImageJobEvent, ImageJobState

_TERMINAL = frozenset({ImageJobState.SUCCEEDED, ImageJobState.FAILED, ImageJobState.CANCELLED})


async def request_image_job_cancel(
    session: AsyncSession, principal: Principal, job_id: str
) -> tuple[ImageJob, bool]:
    """Commit cancel intent and audit; return whether termination is already proved."""
    return await _request_cancel(session, principal, job_id, admin=False)


async def request_admin_image_job_cancel(
    session: AsyncSession, principal: Principal, job_id: str
) -> tuple[ImageJob, bool]:
    """Human-admin kill switch; it also works after owner entitlement revocation."""
    if (
        not principal.is_admin
        or principal.kind not in {PrincipalKind.USER, PrincipalKind.ADMIN}
        or principal.user_id is None
    ):
        raise ImageForbidden()
    return await _request_cancel(session, principal, job_id, admin=True)


async def _request_cancel(
    session: AsyncSession, principal: Principal, job_id: str, *, admin: bool
) -> tuple[ImageJob, bool]:
    # Admission takes this advisory lock before a job lock. Keep the same order.
    await session.execute(_QUOTA_LOCK)
    row = await session.get(ImageJobRow, job_id, populate_existing=True, with_for_update=True)
    if row is None or (not admin and row.owner_user_id != principal.user_id):
        raise ImageNotFound()
    snapshot, required, explicit = _policy(row)
    if not admin:
        await authorize_live_image_action(
            session, principal, explicit=explicit, required_entitlements=required
        )
    try:
        state = ImageJobState(row.state)
    except ValueError as exc:
        raise ImageConflict("image job state unavailable") from exc
    if state in _TERMINAL or state is ImageJobState.CANCELLING:
        if not admin:
            return await get_owned_image_job(session, principal, job_id), state in _TERMINAL
        latest = await session.scalar(
            select(func.max(ImageJobEventRow.sequence)).where(ImageJobEventRow.job_id == job_id)
        )
        return ImageJob(
            id=row.id,
            state=state,
            effective_spec=snapshot.effective_spec,
            resolved=snapshot.resolved,
            failure_code=row.safe_failure_code,
            latest_event_sequence=latest or 0,
            created_at=row.created_at,
            updated_at=row.updated_at,
        ), state in _TERMINAL

    latest = await session.scalar(
        select(func.max(ImageJobEventRow.sequence)).where(ImageJobEventRow.job_id == job_id)
    )
    if latest is None or latest < 1:
        raise ImageConflict("image event history unavailable")
    now = datetime.now(UTC)
    queued_without_node = (
        state is ImageJobState.QUEUED
        and row.fence == 0
        and row.selected_node_id is None
        and row.instance_id is None
        and not row.reservation_ids
    )
    if queued_without_node:
        held = row.authorization_snapshot.get("output_hold_bytes")
        if not isinstance(held, int) or isinstance(held, bool) or held <= 0:
            raise ImageConflict("image capacity hold unavailable")
        await release_pending_image_job_capacity(
            session, row.owner_user_id, snapshot.effective_spec.n, held
        )
        row.state = ImageJobState.CANCELLED
        row.finished_at = now
        row.cleanup_state = "cleaned"
        row.receipt_state = "none"
        event = ImageJobEvent(
            job_id=job_id,
            sequence=latest + 1,
            at=now,
            type="cancelled",
            state=ImageJobState.CANCELLED,
        )
        session.add(
            ImageJobEventRow(
                job_id=job_id,
                sequence=event.sequence,
                event_type=event.type,
                payload=event.model_dump(mode="json"),
                created_at=now,
            )
        )
        latest += 1
    else:
        row.state = ImageJobState.CANCELLING
    row.cancel_requested_at = now
    row.updated_at = now
    row.version += 1
    actor, actor_type, actor_user_id = audit_actor(principal)
    await write_audit(
        session,
        actor=actor,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action="image.admin.cancel" if admin else "image.cancel",
        target_type="image_job",
        target_id=job_id,
        outcome=AuditOutcome.OK,
        context={
            "previous_state": state.value,
            "terminal": queued_without_node,
            **({"owner_id": str(row.owner_user_id)} if admin else {}),
        },
    )
    result = ImageJob(
        id=row.id,
        state=ImageJobState(row.state),
        effective_spec=snapshot.effective_spec,
        resolved=snapshot.resolved,
        failure_code=row.safe_failure_code,
        latest_event_sequence=latest,
        created_at=row.created_at,
        updated_at=now,
    )
    await session.commit()
    return result, queued_without_node
