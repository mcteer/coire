from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import PrincipalKind
from coire_api.db import AgentRunRow, RunTokenRow
from coire_api.run_tokens import (
    RUN_TOKEN_PATTERN,
    InvalidRunToken,
    authenticate_run_token,
    charge_run_token,
    hasher,
    mint_run_token,
    revoke_run_token,
    token_material,
    verify_material,
)
from coire_core.models.runs import AgentRunState, RunTokenScope


def test_run_token_has_256_bit_secret_and_hash_verifies() -> None:
    prefix, secret, presented = token_material()
    assert len(prefix) == 12
    assert len(secret) == 43
    assert RUN_TOKEN_PATTERN.fullmatch(presented)
    digest = hasher.hash(secret)
    assert verify_material(digest, secret)
    assert not verify_material(digest, "x" * 43)
    assert secret not in digest


def test_run_token_pattern_rejects_api_keys_and_malformed_values() -> None:
    assert RUN_TOKEN_PATTERN.fullmatch("coire_abcdefghijkl_" + "x" * 43) is None
    assert RUN_TOKEN_PATTERN.fullmatch("coire_run_short_" + "x" * 43) is None


class Session:
    def __init__(self, run: AgentRunRow) -> None:
        self.run = run
        self.token: RunTokenRow | None = None

    async def scalar(self, _: object) -> RunTokenRow | None:
        return self.token

    async def get(self, _: object, identifier: uuid.UUID) -> AgentRunRow | None:
        return self.run if identifier == self.run.id else None

    def add(self, value: object) -> None:
        if isinstance(value, RunTokenRow):
            self.token = value

    async def flush(self) -> None:
        return None


def run_row() -> AgentRunRow:
    return AgentRunRow(
        id=uuid.uuid4(),
        requester_user_id=uuid.uuid4(),
        profile="general",
        primary_model_id=uuid.uuid4(),
        primary_variant_id=uuid.uuid4(),
        workspace_ref="workspace",
        token_scope={},
        state=AgentRunState.RUNNING,
        limits={},
        resource_usage={},
    )


async def test_mint_authenticate_charge_revoke_and_expiry_are_server_authoritative() -> None:
    run = run_row()
    session = Session(run)
    scope = RunTokenScope(
        permitted_model_ids=frozenset({run.primary_model_id}),
        permitted_tools=frozenset({"read_file"}),
        spend_limit_tokens=10,
    )
    typed_session = cast(AsyncSession, session)
    row, presented = await mint_run_token(typed_session, run, scope, ttl_seconds=60)
    row.spent_tokens = 0
    principal = await authenticate_run_token(typed_session, presented)
    assert principal.kind is PrincipalKind.RUN
    assert principal.permitted_model_ids == {run.primary_model_id}
    await charge_run_token(typed_session, run.id, 10)
    with pytest.raises(InvalidRunToken, match="spend"):
        await authenticate_run_token(typed_session, presented)
    row.spent_tokens = 0
    await revoke_run_token(typed_session, run.id)
    with pytest.raises(InvalidRunToken):
        await authenticate_run_token(typed_session, presented)
    row.revoked_at = None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(InvalidRunToken):
        await authenticate_run_token(typed_session, presented)
