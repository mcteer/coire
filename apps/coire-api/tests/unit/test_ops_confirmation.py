from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import OpsSessionRow
from coire_api.ops import consume_confirmation, current_session
from coire_api.ops_actions import OpsActionError, _check_precondition, resource_version
from coire_api.ops_tokens import InvalidConfirmation, canonical_action_digest, hash_secret
from coire_core.models.ops import (
    ModelPinAction,
    OpsActionPrecondition,
    OpsProposalState,
    OpsSessionState,
)


def _action(target_id: uuid.UUID, version: str) -> ModelPinAction:
    return ModelPinAction(
        operation="model.pin",
        target_type="model",
        target_id=target_id,
        precondition=OpsActionPrecondition(resource_version=version, expected_state="ready"),
    )


def test_resource_precondition_rejects_state_and_version_changes() -> None:
    updated_at = datetime.now(UTC)
    action = _action(uuid.uuid4(), resource_version(updated_at))
    _check_precondition(action, state="ready", updated_at=updated_at)
    with pytest.raises(OpsActionError, match="state changed") as state_error:
        _check_precondition(action, state="stopped", updated_at=updated_at)
    assert state_error.value.stale
    with pytest.raises(OpsActionError, match="version changed") as version_error:
        _check_precondition(
            action,
            state="ready",
            updated_at=updated_at + timedelta(microseconds=1),
        )
    assert version_error.value.stale


@pytest.mark.asyncio
async def test_locked_confirmation_consumes_exactly_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    proposal_id, conversation_id, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    user_id, target_id = uuid.uuid4(), uuid.uuid4()
    action = _action(target_id, "1")
    digest = canonical_action_digest(
        proposal_id=proposal_id,
        conversation_id=conversation_id,
        session_id=session_id,
        action=action,
    )
    prefix, secret = "a" * 12, "b" * 43
    now = datetime.now(UTC)
    token = SimpleNamespace(
        proposal_id=proposal_id,
        secret_hash=hash_secret(secret),
        action_digest=digest,
        used_at=None,
        revoked_at=None,
        expires_at=now + timedelta(minutes=1),
    )
    proposal = SimpleNamespace(
        id=proposal_id,
        conversation_id=conversation_id,
        ops_session_id=session_id,
        state=OpsProposalState.PENDING,
        expires_at=now + timedelta(minutes=1),
        action_digest=digest,
        decided_at=None,
        confirmed_by_user_id=None,
        proposer=f"coire-ops:{session_id}",
    )
    active_session = SimpleNamespace(id=session_id, state=OpsSessionState.ACTIVE)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[token, proposal]),
        flush=AsyncMock(),
    )
    monkeypatch.setattr("coire_api.ops.write_principal_audit", AsyncMock())
    monkeypatch.setattr("coire_api.ops.current_session", AsyncMock(return_value=active_session))
    principal = Principal(
        kind=PrincipalKind.ADMIN,
        subject=str(user_id),
        user_id=user_id,
    )
    presented = f"coire_confirm_{prefix}_{secret}"
    result = await consume_confirmation(
        session,  # type: ignore[arg-type]
        proposal_id=proposal_id,
        presented_token=presented,
        presented_action=action,
        principal=principal,
        stale_seconds=30.0,
    )
    assert result.id == proposal_id
    assert token.used_at is not None
    assert proposal.state is OpsProposalState.CONFIRMED
    assert proposal.confirmed_by_user_id == user_id
    assert all(call.args[0]._for_update_arg is not None for call in session.scalar.await_args_list)

    replay_session = SimpleNamespace(scalar=AsyncMock(side_effect=[token, proposal]))
    with pytest.raises(InvalidConfirmation) as replay:
        await consume_confirmation(
            replay_session,  # type: ignore[arg-type]
            proposal_id=proposal_id,
            presented_token=presented,
            presented_action=action,
            principal=principal,
            stale_seconds=30.0,
        )
    assert replay.value.reason == "used"


@pytest.mark.asyncio
async def test_stale_session_is_superseded_and_pending_authority_revoked() -> None:
    now = datetime.now(UTC)
    session_row = OpsSessionRow(
        id=uuid.uuid4(),
        service_instance="stale-test",
        state=OpsSessionState.ACTIVE,
        started_at=now - timedelta(minutes=1),
        last_seen_at=now - timedelta(seconds=31),
    )
    proposal = SimpleNamespace(
        id=uuid.uuid4(),
        state=OpsProposalState.PENDING,
        decided_at=None,
    )
    token = SimpleNamespace(revoked_at=None)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[session_row, token]),
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [proposal])),
        flush=AsyncMock(),
    )

    assert await current_session(session, stale_seconds=30.0) is None  # type: ignore[arg-type]
    assert session_row.state is OpsSessionState.SUPERSEDED
    assert session_row.ended_at is not None
    assert proposal.state is OpsProposalState.EXPIRED
    assert proposal.decided_at is not None
    assert token.revoked_at is not None
    session.flush.assert_awaited_once()
