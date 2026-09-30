"""Human-admin image preset mutations with live authority and atomic audit."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.auth import CurrentAdmin, Principal, PrincipalKind, audit_actor
from coire_api.db import UserRow, session_scope
from coire_api.deps import SessionDep
from coire_api.images import admin_presets
from coire_api.images.authorization import authorize_live_image_action, preflight_image_action
from coire_api.images.telemetry import (
    ImageOperation,
    ImageOutcome,
    ImageReason,
    image_span,
    record_image_request,
)
from coire_core.errors import CoireError, ImageConflict, ImageForbidden
from coire_core.models.audit import AuditOutcome
from coire_core.models.auth import UserRole
from coire_core.models.images import ImagePreset, ImagePresetCreate, ImagePresetUpdate
from coire_core.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin/image-presets", tags=["admin: images"])
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
                action="image.preset.admin_refused",
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
