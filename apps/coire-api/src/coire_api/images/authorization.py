"""User-bound image authority with a fresh database check at every action boundary."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ApiKeyRow, EntitlementRow, UserRow
from coire_core.errors import ImageForbidden

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
