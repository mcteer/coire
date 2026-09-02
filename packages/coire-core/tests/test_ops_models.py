from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from coire_core.models.ops import (
    InstanceLoadAction,
    InstanceLoadParameters,
    OpsActionPrecondition,
    OpsConfirmRequest,
    resolved_ops_action_adapter,
)


def _precondition() -> OpsActionPrecondition:
    return OpsActionPrecondition(resource_version="42", expected_state="ready")


def test_every_allowlisted_action_has_an_exact_shape() -> None:
    target_id = uuid.uuid4()
    variant_id = uuid.uuid4()
    examples = [
        ("instance.unload", "instance", {}),
        ("run.kill", "run", {}),
        ("model.pin", "model", {}),
        ("model.unpin", "model", {}),
        ("instance.load", "model", {"variant_id": str(variant_id)}),
    ]

    for operation, target_type, parameters in examples:
        action = resolved_ops_action_adapter.validate_python(
            {
                "operation": operation,
                "target_type": target_type,
                "target_id": str(target_id),
                "parameters": parameters,
                "precondition": {"resource_version": "42", "expected_state": "ready"},
            }
        )
        assert action.operation == operation
        assert action.target_id == target_id


@pytest.mark.parametrize(
    "operation",
    [
        "model.retire",
        "model.acquire",
        "user.delete",
        "entitlement.update",
        "shell.exec",
        "route.call",
    ],
)
def test_irreversible_and_general_admin_actions_are_unrepresentable(operation: str) -> None:
    with pytest.raises(ValidationError):
        resolved_ops_action_adapter.validate_python(
            {
                "operation": operation,
                "target_type": "model",
                "target_id": str(uuid.uuid4()),
                "parameters": {},
                "precondition": {"resource_version": "1", "expected_state": "ready"},
            }
        )


def test_action_rejects_target_substitution_and_arbitrary_parameters() -> None:
    common = {
        "operation": "run.kill",
        "target_id": str(uuid.uuid4()),
        "precondition": {"resource_version": "1", "expected_state": "running"},
    }
    with pytest.raises(ValidationError):
        resolved_ops_action_adapter.validate_python(
            {**common, "target_type": "model", "parameters": {}}
        )
    with pytest.raises(ValidationError):
        resolved_ops_action_adapter.validate_python(
            {**common, "target_type": "run", "parameters": {"signal": 9}}
        )


def test_load_requires_a_registry_variant_uuid_and_forbids_paths() -> None:
    with pytest.raises(ValidationError):
        InstanceLoadAction(
            operation="instance.load",
            target_type="model",
            target_id=uuid.uuid4(),
            parameters=InstanceLoadParameters.model_validate(
                {"variant_id": str(uuid.uuid4()), "repository": "attacker/model"}
            ),
            precondition=_precondition(),
        )


def test_confirmation_token_is_bounded_and_hidden_from_repr() -> None:
    token = f"coire_confirm_{'a' * 12}_{'b' * 43}"
    request = OpsConfirmRequest(
        confirm_token=token,
        action=resolved_ops_action_adapter.validate_python(
            {
                "operation": "model.pin",
                "target_type": "model",
                "target_id": str(uuid.uuid4()),
                "parameters": {},
                "precondition": {"resource_version": "1", "expected_state": "ready"},
            }
        ),
    )
    assert token not in repr(request)
    with pytest.raises(ValidationError):
        OpsConfirmRequest.model_validate(
            {**request.model_dump(), "confirm_token": "not-a-confirmation-token"}
        )
