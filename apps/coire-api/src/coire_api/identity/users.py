"""Transactional local-user lifecycle and last-administrator invariant."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import EntitlementRow, UserRow
from coire_core.models.auth import ActorType, User, UserCreate, UserRole, UserUpdate

ADMIN_LOCK_ID = 0x434F49524541444D  # "COIREADM", stable signed bigint


class UserNotFound(LookupError):
    pass


class LastAdminError(ValueError):
    pass


async def project_user(session: AsyncSession, row: UserRow) -> User:
    names = list(
        (
            await session.scalars(
                select(EntitlementRow.name)
                .where(EntitlementRow.user_id == row.id, EntitlementRow.revoked_at.is_(None))
                .order_by(EntitlementRow.name)
            )
        ).all()
    )
    return User(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        role=row.role,
        active=row.active,
        entitlements=names,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> UserRow:
    row = await session.get(UserRow, user_id)
    if row is None:
        raise UserNotFound(str(user_id))
    return row


async def list_users(session: AsyncSession) -> list[User]:
    rows = list((await session.scalars(select(UserRow).order_by(UserRow.email))).all())
    return [await project_user(session, row) for row in rows]


async def create_user(
    session: AsyncSession,
    request: UserCreate,
    *,
    actor: str,
    actor_type: ActorType = ActorType.SERVICE,
    actor_user_id: uuid.UUID | None = None,
) -> User:
    row = UserRow(
        email=request.email,
        display_name=request.display_name,
        role=request.role,
        active=True,
    )
    session.add(row)
    await session.flush()
    await write_audit(
        session,
        actor=actor,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action="user.create",
        target_type="user",
        target_id=str(row.id),
        after={"email": row.email, "role": row.role.value, "active": True},
    )
    return await project_user(session, row)


async def update_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    request: UserUpdate,
    *,
    actor: str,
    actor_type: ActorType = ActorType.SERVICE,
    actor_user_id: uuid.UUID | None = None,
) -> User:
    await session.execute(select(func.pg_advisory_xact_lock(ADMIN_LOCK_ID)))
    row = await get_user(session, user_id)
    before = {"display_name": row.display_name, "role": row.role.value, "active": row.active}
    next_role = request.role if request.role is not None else row.role
    next_active = request.active if request.active is not None else row.active
    removes_admin = (
        row.active
        and row.role is UserRole.ADMIN
        and (not next_active or next_role is not UserRole.ADMIN)
    )
    if removes_admin:
        count = await session.scalar(
            select(func.count(UserRow.id)).where(
                UserRow.active.is_(True), UserRow.role == UserRole.ADMIN, UserRow.id != row.id
            )
        )
        if int(count or 0) == 0:
            raise LastAdminError("the last active administrator cannot be removed")
    if request.display_name is not None:
        row.display_name = request.display_name
    row.role = next_role
    row.active = next_active
    row.updated_at = datetime.now(UTC)
    after = {"display_name": row.display_name, "role": row.role.value, "active": row.active}
    await write_audit(
        session,
        actor=actor,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action="user.update" if row.active else "user.deactivate",
        target_type="user",
        target_id=str(row.id),
        before=before,
        after=after,
    )
    return await project_user(session, row)


async def deactivate_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    actor: str,
    actor_type: ActorType = ActorType.SERVICE,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    await update_user(
        session,
        user_id,
        UserUpdate(active=False),
        actor=actor,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
    )
