"""Idempotently seed the explicitly configured first administrator."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import UserRow
from coire_core.models.auth import UserRole, normalize_email
from coire_core.settings import Settings


async def ensure_bootstrap_admin(session: AsyncSession, settings: Settings) -> UserRow | None:
    configured = settings.bootstrap_admin_email.get_secret_value().strip()
    if not configured:
        return None
    email = normalize_email(configured)
    row = await session.scalar(select(UserRow).where(UserRow.email == email).with_for_update())
    created = row is None
    if row is None:
        row = UserRow(email=email, display_name=email.split("@", 1)[0], role=UserRole.ADMIN)
        session.add(row)
        await session.flush()
    elif row.active and row.role is UserRole.ADMIN:
        return row
    else:
        row.active = True
        row.role = UserRole.ADMIN
    await write_audit(
        session,
        actor="system:bootstrap",
        action="identity.admin.bootstrap",
        target_type="user",
        target_id=str(row.id),
        detail={"created": created, "email": email},
    )
    return row
