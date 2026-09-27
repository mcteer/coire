"""One-time API-key issuance and database-authoritative verification."""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Protocol

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import ApiKeyRow, EntitlementRow, UsageAccumulatorRow, UserRow
from coire_api.identity.windows import month_window
from coire_core.models.auth import (
    ActorType,
    ApiKey,
    ApiKeyCreate,
    ApiKeyIssued,
    ApiKeyUpdate,
    AuthPrincipal,
    AuthScope,
)

KEY_PATTERN = re.compile(r"^coire_([A-Za-z0-9_-]{12})_([A-Za-z0-9_-]{43})$")
# OWASP's Argon2id baseline (19 MiB, two iterations, one lane) keeps verification inside the
# gateway's latency budget while remaining deliberately memory-hard. API-key secrets carry
# 256 random bits, so password-derived low-entropy tradeoffs do not apply here.
hasher = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1)


class CredentialPrincipal(Protocol):
    api_key_id: uuid.UUID | None
    user_id: uuid.UUID | None
    credential_version: int | None


class InvalidApiKey(ValueError):
    pass


class KeyNotFound(LookupError):
    pass


def _material() -> tuple[str, str, str]:
    prefix = secrets.token_urlsafe(9)
    secret = secrets.token_urlsafe(32)
    return prefix, secret, f"coire_{prefix}_{secret}"


async def _consumed(session: AsyncSession, key_id: uuid.UUID) -> tuple[int, datetime]:
    start, end = month_window()
    row = await session.get(UsageAccumulatorRow, (key_id, start))
    return (row.prompt_tokens + row.completion_tokens if row else 0), end


async def project_key(session: AsyncSession, row: ApiKeyRow) -> ApiKey:
    consumed, reset = await _consumed(session, row.id)
    return ApiKey(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        prefix=row.prefix,
        scopes=frozenset(AuthScope(item) for item in row.scopes),
        requests_per_minute=row.requests_per_minute,
        monthly_budget_tokens=row.monthly_budget_tokens,
        tokens_consumed=consumed,
        period_resets_at=reset,
        active=row.revoked_at is None,
        created_at=row.created_at,
        rotated_at=row.rotated_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


async def issue_key(
    session: AsyncSession,
    user_id: uuid.UUID,
    request: ApiKeyCreate,
    *,
    actor: str,
    actor_type: ActorType = ActorType.SERVICE,
    actor_user_id: uuid.UUID | None = None,
) -> ApiKeyIssued:
    user = await session.get(UserRow, user_id)
    if user is None or not user.active:
        raise KeyNotFound("active key owner not found")
    prefix, secret, presented = _material()
    row = ApiKeyRow(
        user_id=user_id,
        name=request.name,
        prefix=prefix,
        secret_hash=hasher.hash(secret),
        credential_version=1,
        scopes=[scope.value for scope in sorted(request.scopes)],
        requests_per_minute=request.requests_per_minute,
        monthly_budget_tokens=request.monthly_budget_tokens,
    )
    session.add(row)
    await session.flush()
    await write_audit(
        session,
        actor=actor,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action="api_key.create",
        target_type="api_key",
        target_id=str(row.id),
        detail={"prefix": prefix, "scopes": row.scopes, "user_id": str(user_id)},
    )
    return ApiKeyIssued(**(await project_key(session, row)).model_dump(), secret=presented)


async def list_keys(session: AsyncSession, user_id: uuid.UUID) -> list[ApiKey]:
    rows = list(
        (
            await session.scalars(
                select(ApiKeyRow).where(ApiKeyRow.user_id == user_id).order_by(ApiKeyRow.created_at)
            )
        ).all()
    )
    return [await project_key(session, row) for row in rows]


async def update_key(
    session: AsyncSession,
    key_id: uuid.UUID,
    request: ApiKeyUpdate,
    *,
    actor: str,
    actor_type: ActorType = ActorType.SERVICE,
    actor_user_id: uuid.UUID | None = None,
) -> ApiKey:
    row = await session.scalar(select(ApiKeyRow).where(ApiKeyRow.id == key_id).with_for_update())
    if row is None or row.revoked_at is not None:
        raise KeyNotFound(str(key_id))
    before = {
        "name": row.name,
        "scopes": list(row.scopes),
        "requests_per_minute": row.requests_per_minute,
        "monthly_budget_tokens": row.monthly_budget_tokens,
    }
    if request.name is not None:
        row.name = request.name
    if request.scopes is not None:
        row.scopes = [scope.value for scope in sorted(request.scopes)]
    if request.requests_per_minute is not None:
        row.requests_per_minute = request.requests_per_minute
    if request.monthly_budget_tokens is not None:
        row.monthly_budget_tokens = request.monthly_budget_tokens
    after = {
        "name": row.name,
        "scopes": list(row.scopes),
        "requests_per_minute": row.requests_per_minute,
        "monthly_budget_tokens": row.monthly_budget_tokens,
    }
    await write_audit(
        session,
        actor=actor,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action="api_key.update",
        target_type="api_key",
        target_id=str(row.id),
        before=before,
        after=after,
    )
    return await project_key(session, row)


async def rotate_key(
    session: AsyncSession,
    key_id: uuid.UUID,
    *,
    actor: str,
    actor_type: ActorType = ActorType.SERVICE,
    actor_user_id: uuid.UUID | None = None,
) -> ApiKeyIssued:
    row = await session.scalar(select(ApiKeyRow).where(ApiKeyRow.id == key_id).with_for_update())
    if row is None or row.revoked_at is not None:
        raise KeyNotFound(str(key_id))
    prefix, secret, presented = _material()
    row.prefix = prefix
    row.secret_hash = hasher.hash(secret)
    row.credential_version += 1
    row.rotated_at = datetime.now(UTC)
    await write_audit(
        session,
        actor=actor,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action="api_key.rotate",
        target_type="api_key",
        target_id=str(row.id),
        detail={"prefix": prefix, "credential_version": row.credential_version},
    )
    return ApiKeyIssued(**(await project_key(session, row)).model_dump(), secret=presented)


async def revoke_key(
    session: AsyncSession,
    key_id: uuid.UUID,
    *,
    actor: str,
    actor_type: ActorType = ActorType.SERVICE,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    row = await session.scalar(select(ApiKeyRow).where(ApiKeyRow.id == key_id).with_for_update())
    if row is None:
        raise KeyNotFound(str(key_id))
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await write_audit(
            session,
            actor=actor,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            action="api_key.revoke",
            target_type="api_key",
            target_id=str(row.id),
            detail={"prefix": row.prefix},
        )


async def authenticate_key(session: AsyncSession, presented: str) -> AuthPrincipal:
    match = KEY_PATTERN.fullmatch(presented)
    if match is None:
        raise InvalidApiKey("invalid API key")
    prefix, secret = match.groups()
    rows = list(
        (
            await session.scalars(
                select(ApiKeyRow).where(ApiKeyRow.prefix == prefix, ApiKeyRow.revoked_at.is_(None))
            )
        ).all()
    )
    matched: ApiKeyRow | None = None
    for row in rows:
        try:
            if hasher.verify(row.secret_hash, secret):
                matched = row
        except VerificationError:
            continue
    if matched is None:
        raise InvalidApiKey("invalid API key")
    user = await session.get(UserRow, matched.user_id)
    if user is None or not user.active:
        raise InvalidApiKey("invalid API key")
    entitlements = frozenset(
        (
            await session.scalars(
                select(EntitlementRow.name).where(
                    EntitlementRow.user_id == user.id, EntitlementRow.revoked_at.is_(None)
                )
            )
        ).all()
    )
    matched.last_used_at = datetime.now(UTC)
    return AuthPrincipal(
        actor_type=ActorType.API_KEY,
        subject=str(matched.id),
        user_id=user.id,
        role=user.role,
        scopes=frozenset(AuthScope(item) for item in matched.scopes),
        entitlements=entitlements,
        api_key_id=matched.id,
        credential_version=matched.credential_version,
    )


async def key_is_active(session: AsyncSession, principal: CredentialPrincipal) -> bool:
    if principal.api_key_id is None:
        return True
    row = await session.get(ApiKeyRow, principal.api_key_id)
    user = await session.get(UserRow, principal.user_id) if principal.user_id else None
    return bool(
        row
        and user
        and user.active
        and row.revoked_at is None
        and row.credential_version == principal.credential_version
    )
