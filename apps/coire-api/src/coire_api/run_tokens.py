"""Server-authoritative, immediately revocable run credentials."""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import AgentRunRow, RunTokenRow
from coire_core.models.runs import TERMINAL_RUN_STATES, AgentRunState, RunTokenScope

RUN_TOKEN_PATTERN = re.compile(r"^coire_run_([A-Za-z0-9_-]{12})_([A-Za-z0-9_-]{43})$")
hasher = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1)


class InvalidRunToken(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"invalid run token: {reason}")


def token_material() -> tuple[str, str, str]:
    prefix = secrets.token_urlsafe(9)
    secret = secrets.token_urlsafe(32)
    return prefix, secret, f"coire_run_{prefix}_{secret}"


def verify_material(secret_hash: str, presented_secret: str) -> bool:
    try:
        return bool(hasher.verify(secret_hash, presented_secret))
    except VerificationError:
        return False


async def mint_run_token(
    session: AsyncSession,
    run: AgentRunRow,
    scope: RunTokenScope,
    *,
    ttl_seconds: int,
) -> tuple[RunTokenRow, str]:
    existing = await session.scalar(select(RunTokenRow).where(RunTokenRow.run_id == run.id))
    if existing is not None:
        raise InvalidRunToken("run token already minted")
    prefix, secret, presented = token_material()
    row = RunTokenRow(
        run_id=run.id,
        prefix=prefix,
        secret_hash=hasher.hash(secret),
        scope=scope.model_dump(mode="json"),
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
    )
    session.add(row)
    await session.flush()
    return row, presented


async def rotate_run_token(
    session: AsyncSession,
    run: AgentRunRow,
    scope: RunTokenScope,
    *,
    ttl_seconds: int,
) -> tuple[RunTokenRow, str]:
    """Mint recoverably without retaining plaintext across command retries.

    A retry first observes the node. If no container exists, rotating this row makes any token
    from an ambiguous prior create attempt invalid before a replacement is sent.
    """
    row = await session.scalar(
        select(RunTokenRow).where(RunTokenRow.run_id == run.id).with_for_update()
    )
    if row is None:
        return await mint_run_token(session, run, scope, ttl_seconds=ttl_seconds)
    prefix, secret, presented = token_material()
    row.prefix = prefix
    row.secret_hash = hasher.hash(secret)
    row.scope = scope.model_dump(mode="json")
    row.spent_tokens = 0
    row.expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    row.revoked_at = None
    await session.flush()
    return row, presented


async def authenticate_run_token(session: AsyncSession, presented: str) -> Principal:
    match = RUN_TOKEN_PATTERN.fullmatch(presented)
    if match is None:
        raise InvalidRunToken("malformed")
    prefix, secret = match.groups()
    row = await session.scalar(select(RunTokenRow).where(RunTokenRow.prefix == prefix))
    if row is None:
        raise InvalidRunToken("unknown_prefix")
    if not verify_material(row.secret_hash, secret):
        raise InvalidRunToken("secret_mismatch")
    run = await session.get(AgentRunRow, row.run_id)
    now = datetime.now(UTC)
    if run is None:
        raise InvalidRunToken("run_missing")
    if run.state in TERMINAL_RUN_STATES or run.state is AgentRunState.KILL_REQUESTED:
        raise InvalidRunToken("run_inactive")
    if row.revoked_at is not None:
        raise InvalidRunToken("revoked")
    if row.expires_at <= now:
        raise InvalidRunToken("expired")
    scope = RunTokenScope.model_validate(row.scope)
    if row.spent_tokens >= scope.spend_limit_tokens:
        raise InvalidRunToken("spend_exhausted")
    return Principal(
        kind=PrincipalKind.RUN,
        subject=str(run.id),
        user_id=run.requester_user_id,
        scopes=frozenset({"chat"}),
        run_id=run.id,
        permitted_model_ids=scope.permitted_model_ids,
        permitted_tools=scope.permitted_tools,
        spend_limit_tokens=scope.spend_limit_tokens,
        spent_tokens=row.spent_tokens,
    )


async def revoke_run_token(session: AsyncSession, run_id: uuid.UUID) -> None:
    row = await session.scalar(
        select(RunTokenRow).where(RunTokenRow.run_id == run_id).with_for_update()
    )
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)


async def charge_run_token(session: AsyncSession, run_id: uuid.UUID, tokens: int) -> None:
    if tokens < 0:
        raise ValueError("token charge cannot be negative")
    row = await session.scalar(
        select(RunTokenRow).where(RunTokenRow.run_id == run_id).with_for_update()
    )
    run = await session.get(AgentRunRow, run_id)
    if (
        row is None
        or run is None
        or row.revoked_at is not None
        or row.expires_at <= datetime.now(UTC)
        or run.state in TERMINAL_RUN_STATES
        or run.state is AgentRunState.KILL_REQUESTED
    ):
        raise InvalidRunToken("invalid run token")
    scope = RunTokenScope.model_validate(row.scope)
    if row.spent_tokens + tokens > scope.spend_limit_tokens:
        raise InvalidRunToken("run spend exhausted")
    row.spent_tokens += tokens


async def run_token_is_active(session: AsyncSession, run_id: uuid.UUID) -> bool:
    row = await session.scalar(select(RunTokenRow).where(RunTokenRow.run_id == run_id))
    run = await session.get(AgentRunRow, run_id)
    now = datetime.now(UTC)
    return bool(
        row is not None
        and run is not None
        and row.revoked_at is None
        and row.expires_at > now
        and run.state not in TERMINAL_RUN_STATES
        and run.state is not AgentRunState.KILL_REQUESTED
    )


async def settle_run_token_usage(
    session: AsyncSession, run_id: uuid.UUID, tokens: int, *, reserved_tokens: int
) -> None:
    """Account already-served usage even when a concurrent kill revoked the credential."""
    if tokens < 0 or reserved_tokens < 0:
        raise ValueError("token charge cannot be negative")
    row = await session.scalar(
        select(RunTokenRow).where(RunTokenRow.run_id == run_id).with_for_update()
    )
    if row is None:
        raise InvalidRunToken("invalid run token")
    scope = RunTokenScope.model_validate(row.scope)
    settled = row.spent_tokens - reserved_tokens + tokens
    if settled < 0 or settled > scope.spend_limit_tokens:
        raise InvalidRunToken("run spend exhausted")
    row.spent_tokens = settled
