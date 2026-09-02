from __future__ import annotations

import uuid

import pytest

from coire_api.ops_tokens import (
    InvalidConfirmation,
    canonical_action_digest,
    hash_secret,
    parse_token,
    token_material,
    verify_secret,
)
from coire_core.models.ops import ModelPinAction, OpsActionPrecondition


def _action(*, target_id: uuid.UUID | None = None, version: str = "1") -> ModelPinAction:
    return ModelPinAction(
        operation="model.pin",
        target_type="model",
        target_id=target_id or uuid.uuid4(),
        precondition=OpsActionPrecondition(resource_version=version, expected_state="ready"),
    )


def test_token_material_round_trips_without_persisting_plaintext() -> None:
    prefix, secret, presented = token_material()
    assert parse_token(presented) == (prefix, secret)
    digest = hash_secret(secret)
    assert secret not in digest
    assert verify_secret(digest, secret)
    assert not verify_secret(digest, f"{secret}x")


@pytest.mark.parametrize("presented", ["", "coire_confirm_x_y", "coire_run_aaaaaaaaaaaa_x"])
def test_malformed_token_has_one_bounded_reason(presented: str) -> None:
    with pytest.raises(InvalidConfirmation) as exc:
        parse_token(presented)
    assert exc.value.reason == "malformed"
    if presented:
        assert presented not in str(exc.value)


def test_canonical_digest_binds_every_authority_field() -> None:
    proposal_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    session_id = uuid.uuid4()
    target_id = uuid.uuid4()
    baseline = canonical_action_digest(
        proposal_id=proposal_id,
        conversation_id=conversation_id,
        session_id=session_id,
        action=_action(target_id=target_id),
    )
    variants = [
        (uuid.uuid4(), conversation_id, session_id, _action(target_id=target_id)),
        (proposal_id, uuid.uuid4(), session_id, _action(target_id=target_id)),
        (proposal_id, conversation_id, uuid.uuid4(), _action(target_id=target_id)),
        (proposal_id, conversation_id, session_id, _action()),
        (proposal_id, conversation_id, session_id, _action(target_id=target_id, version="2")),
    ]
    assert all(
        canonical_action_digest(
            proposal_id=p,
            conversation_id=c,
            session_id=s,
            action=a,
        )
        != baseline
        for p, c, s, a in variants
    )


def test_digest_is_stable_across_equivalent_validations() -> None:
    proposal_id, conversation_id, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    action = _action()
    restored = ModelPinAction.model_validate(action.model_dump(mode="json"))
    assert canonical_action_digest(
        proposal_id=proposal_id,
        conversation_id=conversation_id,
        session_id=session_id,
        action=action,
    ) == canonical_action_digest(
        proposal_id=proposal_id,
        conversation_id=conversation_id,
        session_id=session_id,
        action=restored,
    )
