"""Human-admin image preset mutations with live authority and atomic audit."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from pydantic import AwareDatetime
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.auth import CurrentAdmin, Principal, PrincipalKind, audit_actor
from coire_api.db import (
    ImageExecutionLeaseRow,
    ImageJobRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    NodeRow,
    UserRow,
    session_scope,
)
from coire_api.deps import SessionDep
from coire_api.images import admin_presets, cancellation, coexistence
from coire_api.images.authorization import authorize_live_image_action, preflight_image_action
from coire_api.images.jobs import _policy
from coire_api.images.quota import _QUOTA_LOCK
from coire_api.images.telemetry import (
    ImageOperation,
    ImageOutcome,
    ImageReason,
    image_span,
    record_image_request,
)
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.placement.service import node_admission_lock
from coire_core.errors import (
    CoireError,
    ImageConflict,
    ImageForbidden,
    ImageNotFound,
    ImageValidationError,
)
from coire_core.models.audit import AuditOutcome
from coire_core.models.auth import UserRole
from coire_core.models.console import CursorPage, ImageActivityItem
from coire_core.models.files import ULID_PATTERN
from coire_core.models.image_worker import ImageWorkerLoadResult, ImageWorkerUnloadRequest
from coire_core.models.images import (
    ImageCoexistenceProfile,
    ImageCoexistenceReportRequest,
    ImagePreset,
    ImagePresetCreate,
    ImagePresetUpdate,
)
from coire_core.models.instance import InstanceState
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin/image-presets", tags=["admin: images"])
jobs_router = APIRouter(prefix="/api/v1/admin/image-jobs", tags=["admin: images"])
workers_router = APIRouter(prefix="/api/v1/admin/image-workers", tags=["admin: images"])
coexistence_router = APIRouter(
    prefix="/api/v1/admin/image-coexistence-profiles", tags=["admin: images"]
)
_HUMAN_KINDS = frozenset({PrincipalKind.USER, PrincipalKind.ADMIN})


async def _audit_refusal(principal: Principal, method: str, path: str) -> None:
    actor, actor_type, actor_user_id = audit_actor(principal)
    try:
        async with session_scope() as session:
            await write_audit(
                session,
                actor=actor,
                actor_type=actor_type,
                actor_user_id=actor_user_id,
                action=(
                    "image.coexistence.admin_refused"
                    if "/image-coexistence-profiles" in path
                    else "image.preset.admin_refused"
                ),
                target_type="route",
                target_id=f"{method} {path}",
                outcome=AuditOutcome.REFUSED,
                context={"reason": "authorization_or_policy"},
            )
    except Exception:
        logger.exception("image preset refusal audit failed")


async def require_human_image_admin(request: Request, principal: CurrentAdmin) -> Principal:
    """Require a live human-admin row and exact browser mutation Origin."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    try:
        if (
            not principal.is_admin
            or principal.kind not in _HUMAN_KINDS
            or principal.user_id is None
        ):
            raise ImageForbidden()
        preflight_image_action(
            principal,
            method=request.method,
            origin=request.headers.get("origin"),
            browser_origin=settings.chat_browser_origin,
        )
        async with session_scope() as session:
            await authorize_live_image_action(session, principal)
            user = await session.get(
                UserRow, principal.user_id, populate_existing=True, with_for_update=True
            )
            if user is None or not user.active or user.role is not UserRole.ADMIN:
                raise ImageForbidden()
    except ImageForbidden:
        await _audit_refusal(principal, request.method, request.url.path)
        record_image_request(
            ImageOperation.PRESET_MUTATION, ImageOutcome.REFUSED, reason=ImageReason.AUTH
        )
        raise
    return principal


HumanImageAdmin = Annotated[Principal, Depends(require_human_image_admin)]


@coexistence_router.post("", response_model=ImageCoexistenceProfile, status_code=201)
async def approve_image_coexistence_profile(
    body: ImageCoexistenceReportRequest,
    request: Request,
    principal: HumanImageAdmin,
    session: SessionDep,
    response: Response,
) -> ImageCoexistenceProfile:
    with image_span(ImageOperation.ADMIN_INSPECT):
        try:
            result = await coexistence.admit_coexistence_report(session, principal, body)
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            await _audit_refusal(principal, request.method, request.url.path)
            record_image_request(
                ImageOperation.ADMIN_INSPECT, ImageOutcome.REFUSED, reason=ImageReason.CONFLICT
            )
            raise ImageConflict("coexistence report already exists") from exc
        except CoireError:
            await session.rollback()
            await _audit_refusal(principal, request.method, request.url.path)
            record_image_request(
                ImageOperation.ADMIN_INSPECT, ImageOutcome.REFUSED, reason=ImageReason.DEPENDENCY
            )
            raise
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.ADMIN_INSPECT, ImageOutcome.SUCCEEDED)
        return result


@coexistence_router.delete("/{profile_id}", response_model=ImageCoexistenceProfile)
async def invalidate_image_coexistence_profile(
    profile_id: uuid.UUID,
    request: Request,
    principal: HumanImageAdmin,
    session: SessionDep,
    response: Response,
) -> ImageCoexistenceProfile:
    with image_span(ImageOperation.ADMIN_CANCEL):
        try:
            result = await coexistence.invalidate_coexistence_profile(
                session, principal, profile_id
            )
            await session.commit()
        except CoireError:
            await session.rollback()
            await _audit_refusal(principal, request.method, request.url.path)
            record_image_request(
                ImageOperation.ADMIN_CANCEL, ImageOutcome.REFUSED, reason=ImageReason.DEPENDENCY
            )
            raise
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.ADMIN_CANCEL, ImageOutcome.SUCCEEDED)
        return result


def _activity(row: ImageJobRow) -> ImageActivityItem:
    snapshot, _, _ = _policy(row)
    terminal = row.state in {"succeeded", "failed", "cancelled"}
    elapsed_end = row.updated_at if terminal else datetime.now(UTC)
    return ImageActivityItem.model_validate(
        {
            "job_id": row.id,
            "owner_id": row.owner_user_id,
            "model_id": snapshot.effective_spec.model_id,
            "state": row.state,
            "started_at": row.queued_at,
            "elapsed_seconds": max(0, (elapsed_end - row.queued_at).total_seconds()),
            "progress_total": snapshot.effective_spec.n * snapshot.effective_spec.steps,
            "safe_failure_code": row.safe_failure_code,
            "can_stop": not terminal,
        }
    )


async def _audit_admin_job(
    session: AsyncSession, principal: Principal, action: str, target_id: str
) -> None:
    actor, actor_type, actor_user_id = audit_actor(principal)
    await write_audit(
        session,
        actor=actor,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action=action,
        target_type="image_job",
        target_id=target_id,
        outcome=AuditOutcome.OK,
    )


@jobs_router.get("", response_model=CursorPage[ImageActivityItem])
async def list_admin_image_jobs(
    principal: HumanImageAdmin,
    session: SessionDep,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    before: AwareDatetime | None = None,
    before_id: str | None = Query(default=None, pattern=ULID_PATTERN),
) -> CursorPage[ImageActivityItem]:
    with image_span(ImageOperation.ADMIN_INSPECT):
        if before_id is not None and before is None:
            raise ImageValidationError("image activity cursor needs a timestamp")
        query = select(ImageJobRow)
        if before is not None:
            query = query.where(
                or_(
                    ImageJobRow.queued_at < before,
                    and_(ImageJobRow.queued_at == before, ImageJobRow.id < before_id),
                )
                if before_id is not None
                else ImageJobRow.queued_at < before
            )
        rows = (
            await session.scalars(
                query.order_by(ImageJobRow.queued_at.desc(), ImageJobRow.id.desc()).limit(limit + 1)
            )
        ).all()
        page = rows[:limit]
        await _audit_admin_job(session, principal, "image.admin.list", "recent")
        await session.commit()
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.ADMIN_INSPECT, ImageOutcome.SUCCEEDED)
        return CursorPage[ImageActivityItem](
            items=[_activity(row) for row in page],
            next_cursor=f"{page[-1].queued_at.isoformat()}|{page[-1].id}"
            if len(rows) > limit and page
            else None,
        )


@jobs_router.get("/{job_id}", response_model=ImageActivityItem)
async def inspect_admin_image_job(
    job_id: Annotated[str, Path(pattern=ULID_PATTERN)],
    principal: HumanImageAdmin,
    session: SessionDep,
    response: Response,
) -> ImageActivityItem:
    with image_span(ImageOperation.ADMIN_INSPECT, job_id=job_id):
        row = await session.get(ImageJobRow, job_id)
        if row is None:
            raise ImageNotFound()
        result = _activity(row)
        await _audit_admin_job(session, principal, "image.admin.inspect", job_id)
        await session.commit()
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.ADMIN_INSPECT, ImageOutcome.SUCCEEDED, job_id=job_id)
        return result


@jobs_router.delete("/{job_id}", response_model=ImageActivityItem, status_code=202)
async def kill_admin_image_job(
    job_id: Annotated[str, Path(pattern=ULID_PATTERN)],
    principal: HumanImageAdmin,
    session: SessionDep,
    response: Response,
) -> ImageActivityItem:
    with image_span(ImageOperation.ADMIN_CANCEL, job_id=job_id):
        _result, terminal = await cancellation.request_admin_image_job_cancel(
            session, principal, job_id
        )
        row = await session.get(ImageJobRow, job_id, populate_existing=True)
        if row is None:
            raise ImageNotFound()
        response.status_code = 200 if terminal else 202
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.ADMIN_CANCEL, ImageOutcome.ACCEPTED, job_id=job_id)
        return _activity(row)


@workers_router.delete("/{instance_id}", response_model=ImageWorkerLoadResult)
async def unload_admin_image_worker(
    instance_id: uuid.UUID,
    request: Request,
    principal: HumanImageAdmin,
    session: SessionDep,
    response: Response,
) -> ImageWorkerLoadResult:
    """Drain an idle image instance, then unload only its exact node-owned process."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    now = datetime.now(UTC)
    with image_span(ImageOperation.ADMIN_CANCEL):
        await session.execute(_QUOTA_LOCK)
        instance = await session.get(
            ModelInstanceRow, instance_id, populate_existing=True, with_for_update=True
        )
        if instance is None or not instance.policy.startswith("image:"):
            raise ImageNotFound()
        if instance.state is InstanceState.STOPPED:
            raise ImageConflict("image worker is already stopped")
        active = await session.scalar(
            select(ImageJobRow.id)
            .where(
                ImageJobRow.instance_id == instance_id,
                ImageJobRow.state.notin_(["succeeded", "failed", "cancelled"]),
            )
            .limit(1)
        )
        if active is not None:
            raise ImageConflict("image worker still has an active job")
        member = await session.scalar(
            select(InstanceMemberRow).where(InstanceMemberRow.instance_id == instance_id).limit(1)
        )
        node = await session.get(NodeRow, member.node_id) if member is not None else None
        if node is None or instance.policy != f"image:{node.name}":
            raise ImageConflict("image worker node binding unavailable")
        uncertain_lease = await session.scalar(
            select(ImageExecutionLeaseRow.id)
            .where(
                ImageExecutionLeaseRow.node_id == node.id,
                ImageExecutionLeaseRow.mode == "image",
                ImageExecutionLeaseRow.released_at.is_(None),
            )
            .limit(1)
        )
        if uncertain_lease is not None:
            raise ImageConflict("image worker still has an unreleased execution lease")
        instance.state = InstanceState.DRAINING
        instance.updated_at = now
        instance.transitioned_at = now
        await _audit_admin_job(
            session, principal, "image.worker.unload_requested", str(instance_id)
        )
        await session.commit()

        command = ImageWorkerUnloadRequest(
            instance_id=instance_id, reason="admin", requested_at=now
        )
        try:
            async with NodeClient(settings) as client:
                result = await client.unload_image_worker(node.name, command)
        except NodeError as exc:
            raise ImageConflict("image worker stop needs reconciliation") from exc
        if (
            result.instance_id != instance_id
            or result.state != "failed"
            or result.reserved_bytes != 0
            or result.safe_error != "worker_stopped"
        ):
            raise ImageConflict("image worker stop proof differs from request")

        await session.execute(_QUOTA_LOCK)
        async with node_admission_lock(session, node.id):
            locked = await session.get(
                ModelInstanceRow, instance_id, populate_existing=True, with_for_update=True
            )
            if locked is None or locked.state is not InstanceState.DRAINING:
                raise ImageConflict("image worker drain state changed")
            reservation = await session.scalar(
                select(MemoryReservationRow)
                .where(
                    MemoryReservationRow.node_id == node.id,
                    MemoryReservationRow.holder_type == ReservationHolder.IMAGE,
                    MemoryReservationRow.holder_id == str(instance_id),
                )
                .with_for_update()
            )
            if reservation is not None:
                reservation.state = MemoryReservationState.RELEASED
                reservation.released_at = datetime.now(UTC)
            locked.state = InstanceState.STOPPED
            locked.updated_at = datetime.now(UTC)
            locked.transitioned_at = locked.updated_at
            await _audit_admin_job(session, principal, "image.worker.unloaded", str(instance_id))
        await session.commit()
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.ADMIN_CANCEL, ImageOutcome.SUCCEEDED)
        return result


async def _mutate[T](
    session: AsyncSession,
    action: Callable[[], Awaitable[T]],
    principal: Principal,
    request: Request,
) -> T:
    with image_span(ImageOperation.PRESET_MUTATION):
        try:
            result = await action()
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            await _audit_refusal(principal, request.method, request.url.path)
            record_image_request(
                ImageOperation.PRESET_MUTATION,
                ImageOutcome.REFUSED,
                reason=ImageReason.CONFLICT,
            )
            raise ImageConflict() from exc
        except CoireError as exc:
            await session.rollback()
            await _audit_refusal(principal, request.method, request.url.path)
            record_image_request(
                ImageOperation.PRESET_MUTATION,
                ImageOutcome.REFUSED,
                reason=ImageReason.CONFLICT
                if isinstance(exc, ImageConflict)
                else ImageReason.DEPENDENCY,
            )
            raise
        except Exception:
            await session.rollback()
            record_image_request(
                ImageOperation.PRESET_MUTATION,
                ImageOutcome.FAILED,
                reason=ImageReason.INTERNAL,
            )
            raise
        record_image_request(ImageOperation.PRESET_MUTATION, ImageOutcome.ACCEPTED)
        return result


def _user_id(principal: Principal) -> uuid.UUID:
    if principal.user_id is None:
        raise ImageForbidden()
    return principal.user_id


@router.post("", response_model=ImagePreset, status_code=status.HTTP_201_CREATED)
async def create_preset(
    body: ImagePresetCreate,
    request: Request,
    principal: HumanImageAdmin,
    session: SessionDep,
) -> ImagePreset:
    return await _mutate(
        session,
        lambda: admin_presets.create_image_preset(
            session,
            body,
            admin_user_id=_user_id(principal),
            actor=principal.subject or "admin",
        ),
        principal,
        request,
    )


@router.patch("/{preset_id}", response_model=ImagePreset)
async def update_preset(
    preset_id: uuid.UUID,
    body: ImagePresetUpdate,
    request: Request,
    principal: HumanImageAdmin,
    session: SessionDep,
) -> ImagePreset:
    return await _mutate(
        session,
        lambda: admin_presets.update_image_preset(
            session,
            preset_id,
            body,
            admin_user_id=_user_id(principal),
            actor=principal.subject or "admin",
        ),
        principal,
        request,
    )


@router.delete("/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def retire_preset(
    preset_id: uuid.UUID,
    request: Request,
    principal: HumanImageAdmin,
    session: SessionDep,
) -> Response:
    await _mutate(
        session,
        lambda: admin_presets.retire_image_preset(
            session,
            preset_id,
            admin_user_id=_user_id(principal),
            actor=principal.subject or "admin",
        ),
        principal,
        request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
