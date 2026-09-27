"""Audited entitlement grants and revocations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import EntitlementRow, UserRow
from coire_core.models.auth import ActorType


class EntitlementUserNotFound(LookupError):
    pass


async def grant(
    session: AsyncSession,
    user_id: uuid.UUID,
    name: str,
    *,
    granted_by: uuid.UUID,
    actor: str,
    actor_type: ActorType = ActorType.SERVICE,
    actor_user_id: uuid.UUID | None = None,
) -> EntitlementRow:
    user = await session.get(UserRow, user_id)
    if user is None:
        raise EntitlementUserNotFound(str(user_id))
    row = await session.scalar(
        select(EntitlementRow)
        .where(EntitlementRow.user_id == user_id, EntitlementRow.name == name)
        .order_by(EntitlementRow.granted_at.desc())
        .with_for_update()
    )
    if row is None or row.revoked_at is not None:
        row = EntitlementRow(user_id=user_id, name=name, granted_by=granted_by)
        session.add(row)
        await session.flush()
        await write_audit(
            session,
            actor=actor,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            action="entitlement.grant",
            target_type="entitlement",
            target_id=str(row.id),
            after={"user_id": str(user_id), "name": name},
        )
    return row


async def revoke(
    session: AsyncSession,
    user_id: uuid.UUID,
    name: str,
    *,
    actor: str,
    actor_type: ActorType = ActorType.SERVICE,
    actor_user_id: uuid.UUID | None = None,
) -> bool:
    row = await session.scalar(
        select(EntitlementRow)
        .where(
            EntitlementRow.user_id == user_id,
            EntitlementRow.name == name,
            EntitlementRow.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        return False
    row.revoked_at = datetime.now(UTC)
    await write_audit(
        session,
        actor=actor,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action="entitlement.revoke",
        target_type="entitlement",
        target_id=str(row.id),
        before={"user_id": str(user_id), "name": name},
    )
    return True
