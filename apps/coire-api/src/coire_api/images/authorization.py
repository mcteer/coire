"""User-bound image authority with a fresh database check at every action boundary."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.auth import CurrentPrincipal, Principal, PrincipalKind, audit_actor
from coire_api.db import (
    ApiKeyRow,
    EntitlementRow,
    ImageInputRow,
    ImageJobRow,
    ImageOutputRow,
    UserRow,
    session_scope,
)
from coire_core.errors import ImageForbidden, ImageNotFound
from coire_core.models.audit import AuditOutcome
from coire_core.models.images import ImageContentMode, ImageContentTag
from coire_core.settings import get_settings

logger = logging.getLogger(__name__)

_HUMAN_KINDS = frozenset({PrincipalKind.USER, PrincipalKind.ADMIN})
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def preflight_image_action(
    principal: Principal,
    *,
    method: str,
    origin: str | None,
    browser_origin: str,
    explicit: bool = False,
) -> uuid.UUID:
    """Refuse non-human and unscoped callers before touching image state."""
    owner_id = principal.user_id
    if owner_id is None or principal.kind not in (*_HUMAN_KINDS, PrincipalKind.API_KEY):
        raise ImageForbidden()
    if principal.kind is PrincipalKind.API_KEY:
        if principal.api_key_id is None or principal.credential_version is None:
            raise ImageForbidden()
        if "images" not in principal.scopes:
            raise ImageForbidden()
        if explicit and "images:explicit" not in principal.scopes:
            raise ImageForbidden()
    elif method not in _READ_METHODS:
        if not browser_origin or origin != browser_origin:
            raise ImageForbidden()
    return owner_id


async def authorize_live_image_action(
    session: AsyncSession,
    principal: Principal,
    *,
    explicit: bool = False,
    required_entitlements: frozenset[str] = frozenset(),
) -> uuid.UUID:
    """Recheck active user, personal key and entitlement union in the current transaction."""
    owner_id = preflight_image_action(
        principal, method="GET", origin=None, browser_origin="", explicit=explicit
    )
    user = await session.get(UserRow, owner_id, populate_existing=True, with_for_update=True)
    if user is None or not user.active:
        raise ImageForbidden()
    if principal.kind is PrincipalKind.API_KEY:
        key = await session.get(
            ApiKeyRow, principal.api_key_id, populate_existing=True, with_for_update=True
        )
        if (
            key is None
            or key.user_id != owner_id
            or key.revoked_at is not None
            or key.credential_version != principal.credential_version
            or "images" not in key.scopes
            or (explicit and "images:explicit" not in key.scopes)
        ):
            raise ImageForbidden()
    required = required_entitlements | ({"explicit"} if explicit else set())
    if required:
        active = frozenset(
            (
                await session.scalars(
                    select(EntitlementRow.name)
                    .where(
                        EntitlementRow.user_id == owner_id,
                        EntitlementRow.revoked_at.is_(None),
                    )
                    .with_for_update()
                )
            ).all()
        )
        if not required <= active:
            raise ImageForbidden()
    return owner_id


async def require_image_principal(request: Request, principal: CurrentPrincipal) -> Principal:
    """Reusable route guard; refusal audit commits outside the rejected request transaction."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    try:
        preflight_image_action(
            principal,
            method=request.method,
            origin=request.headers.get("origin"),
            browser_origin=settings.chat_browser_origin,
        )
        async with session_scope() as session:
            await authorize_live_image_action(session, principal)
    except ImageForbidden:
        actor, actor_type, actor_user_id = audit_actor(principal)
        try:
            async with session_scope() as audit_session:
                await write_audit(
                    audit_session,
                    actor=actor,
                    actor_type=actor_type,
                    actor_user_id=actor_user_id,
                    action="image.refused",
                    target_type="route",
                    target_id=f"{request.method} {request.url.path}",
                    outcome=AuditOutcome.REFUSED,
                    context={"reason": "authorization"},
                )
        except Exception:
            logger.exception("image refusal audit failed")
        raise
    return principal


CurrentImageUser = Annotated[Principal, Depends(require_image_principal)]


async def require_owned_image_job(
    session: AsyncSession, job_id: str, principal: Principal
) -> ImageJobRow:
    row = await session.get(ImageJobRow, job_id)
    if row is None or row.owner_user_id != principal.user_id:
        raise ImageNotFound()
    return row


async def require_owned_image_input(
    session: AsyncSession, input_id: uuid.UUID, principal: Principal
) -> ImageInputRow:
    row = await session.get(ImageInputRow, input_id)
    if (
        row is None
        or row.owner_user_id != principal.user_id
        or row.deleted_at is not None
        or row.state in {"deleting", "purged"}
    ):
        raise ImageNotFound()
    return row


async def require_owned_image_output(
    session: AsyncSession,
    output_id: uuid.UUID,
    principal: Principal,
    *,
    lock: bool = False,
) -> ImageOutputRow:
    if lock:
        row = await session.get(
            ImageOutputRow, output_id, populate_existing=True, with_for_update=True
        )
    else:
        row = await session.get(ImageOutputRow, output_id)
    if (
        row is None
        or row.owner_user_id != principal.user_id
        or row.state != "published"
        or row.deleted_at is not None
    ):
        raise ImageNotFound()
    return row


def _output_requires_explicit(row: ImageOutputRow) -> bool:
    """Read persisted policy defensively; classification cannot downgrade explicit mode."""
    recipe = row.recipe
    resolved = recipe.get("resolved") if isinstance(recipe, dict) else None
    spec = resolved.get("spec") if isinstance(resolved, dict) else None
    mode = spec.get("content_mode") if isinstance(spec, dict) else None
    if mode not in (ImageContentMode.STANDARD, ImageContentMode.EXPLICIT):
        raise ImageForbidden()
    if row.content_tag not in (
        ImageContentTag.NORMAL,
        ImageContentTag.EXPLICIT,
        ImageContentTag.UNKNOWN,
    ):
        raise ImageForbidden()
    return mode == ImageContentMode.EXPLICIT or row.content_tag == ImageContentTag.EXPLICIT


def output_is_shareable(row: ImageOutputRow) -> bool:
    """Conservative predicate for any future shared/public projection."""
    if row.state != "published" or row.deleted_at is not None:
        return False
    try:
        return not _output_requires_explicit(row) and row.content_tag == ImageContentTag.NORMAL
    except ImageForbidden:
        return False


async def require_downloadable_image_output(
    session: AsyncSession, output_id: uuid.UUID, principal: Principal
) -> ImageOutputRow:
    """Recheck owner and live explicit authority when issuing or redeeming a grant."""
    row = await require_owned_image_output(session, output_id, principal, lock=True)
    await authorize_live_image_action(session, principal, explicit=_output_requires_explicit(row))
    return row
