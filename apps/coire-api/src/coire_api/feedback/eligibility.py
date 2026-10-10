"""Live human credentials and owner-first privacy locks for every contribution."""

from __future__ import annotations

import uuid

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    ApiKeyRow,
    ChatConversationRow,
    EntitlementRow,
    FeedbackPreferenceRow,
    UserRow,
)
from coire_core.errors import FeedbackForbidden, FeedbackNotFound
from coire_core.models.auth import UserRole


async def authorize_owner(session: AsyncSession, principal: Principal) -> uuid.UUID:
    owner = principal.user_id
    if owner is None or principal.kind not in {
        PrincipalKind.USER,
        PrincipalKind.ADMIN,
        PrincipalKind.API_KEY,
    }:
        raise FeedbackForbidden()
    user = await session.get(UserRow, owner, populate_existing=True, with_for_update=True)
    if user is None or not user.active:
        raise FeedbackForbidden()
    if principal.kind is PrincipalKind.API_KEY:
        if principal.api_key_id is None or principal.credential_version is None:
            raise FeedbackForbidden()
        key = await session.get(
            ApiKeyRow, principal.api_key_id, populate_existing=True, with_for_update=True
        )
        if (
            key is None
            or key.user_id != owner
            or key.revoked_at is not None
            or key.credential_version != principal.credential_version
            or "chat" not in key.scopes
        ):
            raise FeedbackForbidden()
    return owner


async def lock_preference(session: AsyncSession, owner: uuid.UUID) -> FeedbackPreferenceRow:
    # A stable owner lock also serializes first materialization against opt-out.
    user = await session.get(UserRow, owner, populate_existing=True, with_for_update=True)
    if user is None or not user.active:
        raise FeedbackForbidden()
    await session.execute(
        insert(FeedbackPreferenceRow).values(owner_user_id=owner).on_conflict_do_nothing()
    )
    preference = await session.get(
        FeedbackPreferenceRow, owner, populate_existing=True, with_for_update=True
    )
    assert preference is not None
    return preference


async def require_generation(
    session: AsyncSession, owner: uuid.UUID, generation: int
) -> FeedbackPreferenceRow:
    preference = await lock_preference(session, owner)
    if not preference.enabled or preference.capture_generation != generation:
        raise FeedbackForbidden("Feedback capture is disabled or this contribution was withdrawn")
    return preference


async def refresh_owner_principal(session: AsyncSession, principal: Principal) -> Principal:
    from sqlalchemy import select

    owner = await authorize_owner(session, principal)
    user = await session.get(UserRow, owner)
    assert user is not None
    scopes = principal.scopes
    if principal.api_key_id is not None:
        key = await session.get(ApiKeyRow, principal.api_key_id)
        assert key is not None
        scopes = frozenset(key.scopes)
    if user.role is not UserRole.ADMIN:
        scopes = scopes - {"admin"}
    kind = principal.kind
    if kind is not PrincipalKind.API_KEY:
        kind = PrincipalKind.ADMIN if user.role is UserRole.ADMIN else PrincipalKind.USER
    entitlements = frozenset(
        await session.scalars(
            select(EntitlementRow.name).where(
                EntitlementRow.user_id == owner, EntitlementRow.revoked_at.is_(None)
            )
        )
    )
    return principal.model_copy(
        update={"kind": kind, "role": user.role, "scopes": scopes, "entitlements": entitlements}
    )


async def lock_conversation(
    session: AsyncSession, owner: uuid.UUID, conversation_id: uuid.UUID
) -> ChatConversationRow:
    row = await session.get(
        ChatConversationRow, conversation_id, populate_existing=True, with_for_update=True
    )
    if row is None or row.owner_user_id != owner or row.deleted_at is not None:
        raise FeedbackNotFound()
    return row


async def authorize_admin(session: AsyncSession, principal: Principal) -> uuid.UUID:
    from coire_api.training.authorization import authorize_live_training_action
    from coire_core.errors import TrainingForbidden

    try:
        return await authorize_live_training_action(session, principal)
    except TrainingForbidden:
        raise FeedbackForbidden() from None


async def lock_owners(session: AsyncSession, owners: set[uuid.UUID]) -> None:
    """Cross-owner review/publication locks include the admin in the same sorted set."""
    from sqlalchemy import select

    if not 1 <= len(owners) <= 10001:
        raise ValueError("feedback owner lock set is out of bounds")
    rows = list(
        await session.scalars(
            select(UserRow)
            .where(UserRow.id.in_(owners))
            .order_by(UserRow.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(rows) != len(owners) or any(not row.active for row in rows):
        raise FeedbackForbidden()
