"""Database-authoritative local browser credentials; no external identity dependency."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import anyio
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException, Request
from opentelemetry import metrics, trace
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import BrowserSessionRow, LocalCredentialRow, LocalLoginWindowRow, UserRow
from coire_core.models.auth import ActorType, normalize_email
from coire_core.settings import Settings

if TYPE_CHECKING:
    from coire_api.auth import Principal

hasher = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1)
limiter = anyio.CapacityLimiter(1)
tracer = trace.get_tracer("coire.api.local_auth")
attempts = metrics.get_meter("coire.api.local_auth").create_counter(
    "coire_local_auth_attempts_total"
)
# Same cost for absent/disabled users; generated once, contains no real credential.
_dummy = hasher.hash(secrets.token_urlsafe(32))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def cookie_name(settings: Settings) -> str:
    return (
        "__Host-coire-session"
        if settings.local_auth_origin.startswith("https://")
        else "coire-dev-session"
    )


def correct_origin(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    expected = settings.local_auth_origin
    return bool(
        request.url.scheme == urlsplit(expected).scheme
        and settings.local_auth_enabled
        and expected
        and request.headers.get("origin") == expected
        and request.headers.get("host") == urlsplit(expected).netloc
    )


async def provision_password(session: AsyncSession, email: str, password: str) -> None:
    """Host operator only: set an existing user's password and invalidate prior sessions."""
    if not 15 <= len(password) <= 1024:
        raise ValueError("password must contain 15 to 1024 characters")
    user = await session.scalar(
        select(UserRow).where(UserRow.email == normalize_email(email)).with_for_update()
    )
    if user is None or not user.active:
        raise ValueError("an existing active user is required")
    encoded = await anyio.to_thread.run_sync(hasher.hash, password, limiter=limiter)
    row = await session.get(LocalCredentialRow, user.id)
    if row is None:
        session.add(LocalCredentialRow(user_id=user.id, password_hash=encoded, generation=1))
    else:
        row.password_hash = encoded
        row.generation += 1
        row.updated_at = datetime.now(UTC)
    await session.execute(
        update(BrowserSessionRow)
        .where(BrowserSessionRow.user_id == user.id)
        .values(revoked_at=datetime.now(UTC))
    )
    await write_audit(
        session,
        actor="operator:local-console",
        action="identity.local.reset",
        target_type="user",
        target_id=str(user.id),
        actor_type=ActorType.SERVICE,
    )


async def throttle(session: AsyncSession, account: str) -> bool:
    now = datetime.now(UTC)
    await session.execute(
        insert(LocalLoginWindowRow)
        .values(account=account, window_start=now, attempts=0)
        .on_conflict_do_nothing()
    )
    row = await session.scalar(
        select(LocalLoginWindowRow).where(LocalLoginWindowRow.account == account).with_for_update()
    )
    assert row is not None
    if now - row.window_start >= timedelta(minutes=1):
        row.window_start, row.attempts = now, 0
    allowed = row.attempts < 5
    if allowed:
        row.attempts += 1
    return allowed


async def verify_login(
    session: AsyncSession, email: str, password: str
) -> tuple[UserRow | None, bool]:
    user = await session.scalar(select(UserRow).where(UserRow.email == email))
    credential = await session.get(LocalCredentialRow, user.id) if user is not None else None
    # Only known credentials get dedicated buckets. Unknown identities cannot create unbounded rows.
    allowed = await throttle(session, digest(email) if credential else "unknown")
    await session.commit()  # Failed verifications must not roll back the throttle.
    if not allowed:
        return None, True
    encoded = (
        credential.password_hash
        if credential is not None and user is not None and user.active
        else _dummy
    )
    valid: bool
    try:
        with tracer.start_as_current_span("coire.api.local_auth.verify"):
            valid = await anyio.to_thread.run_sync(
                hasher.verify, encoded, password, limiter=limiter
            )
    except VerificationError:
        valid = False
    # Reload under lock after the expensive verification: a concurrent reset/deactivation wins.
    if valid and user is not None and credential is not None:
        verified_hash = encoded
        await session.refresh(user, with_for_update=True)
        await session.refresh(credential, with_for_update=True)
        if not user.active or credential.password_hash != verified_hash:
            valid = False
    return user if valid else None, False


async def authenticate_session(request: Request) -> Principal:
    from coire_api.auth import ANONYMOUS, Principal, PrincipalKind
    from coire_api.db import session_scope
    from coire_api.identity.users import project_user
    from coire_core.models.auth import UserRole

    settings: Settings = request.app.state.settings
    token = request.cookies.get(cookie_name(settings), "")
    if (
        not settings.local_auth_enabled
        or not settings.local_auth_origin
        or not token
        or len(token) != 43
    ):
        return ANONYMOUS
    if (
        request.url.scheme != urlsplit(settings.local_auth_origin).scheme
        or request.headers.get("host") != urlsplit(settings.local_auth_origin).netloc
    ):
        return ANONYMOUS
    async with session_scope() as session:
        row = await session.get(BrowserSessionRow, digest(token))
        if row is None or row.revoked_at is not None or row.expires_at <= datetime.now(UTC):
            return ANONYMOUS
        user = await session.get(UserRow, row.user_id)
        credential = await session.get(LocalCredentialRow, row.user_id)
        if (
            user is None
            or not user.active
            or credential is None
            or credential.generation != row.generation
        ):
            return ANONYMOUS
        projected = await project_user(session, user)
        return Principal(
            kind=PrincipalKind.ADMIN if user.role is UserRole.ADMIN else PrincipalKind.USER,
            subject=str(user.id),
            user_id=user.id,
            role=user.role,
            entitlements=frozenset(projected.entitlements),
        )


def require_origin(request: Request) -> None:
    if not correct_origin(request):
        raise HTTPException(403, "same-origin local sign-in required")
