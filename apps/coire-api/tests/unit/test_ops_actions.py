from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from coire_api.auth import Principal, PrincipalKind
from coire_api.ops_actions import ACTION_REGISTRY, execute_action
from coire_core.models.ops import (
    InstanceLoadAction,
    InstanceLoadParameters,
    InstanceUnloadAction,
    ModelPinAction,
    ModelUnpinAction,
    OpsActionPrecondition,
    RunKillAction,
)
from coire_core.settings import Settings


def test_registry_contains_exactly_the_five_reviewed_reversible_actions() -> None:
    assert set(ACTION_REGISTRY) == {
        "instance.unload",
        "run.kill",
        "model.pin",
        "model.unpin",
        "instance.load",
    }
    assert all(spec.reversible for spec in ACTION_REGISTRY.values())


def test_each_registry_entry_has_an_exact_target_and_discriminator_type() -> None:
    expected = {
        "instance.unload": ("instance", InstanceUnloadAction),
        "run.kill": ("run", RunKillAction),
        "model.pin": ("model", ModelPinAction),
        "model.unpin": ("model", ModelUnpinAction),
        "instance.load": ("model", InstanceLoadAction),
    }
    assert {
        operation: (spec.target_type, spec.action_type)
        for operation, spec in ACTION_REGISTRY.items()
    } == expected


def test_registry_has_no_generic_route_or_irreversible_executor() -> None:
    forbidden_fragments = ("delete", "retire", "acquire", "shell", "route", "user")
    assert all(
        fragment not in operation
        for operation in ACTION_REGISTRY
        for fragment in forbidden_fragments
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "handler"),
    [
        (
            InstanceUnloadAction(
                operation="instance.unload",
                target_type="instance",
                target_id=uuid.uuid4(),
                precondition=OpsActionPrecondition(resource_version="1", expected_state="ready"),
            ),
            "_unload_instance",
        ),
        (
            RunKillAction(
                operation="run.kill",
                target_type="run",
                target_id=uuid.uuid4(),
                precondition=OpsActionPrecondition(resource_version="1", expected_state="running"),
            ),
            "_kill_run",
        ),
        (
            ModelPinAction(
                operation="model.pin",
                target_type="model",
                target_id=uuid.uuid4(),
                precondition=OpsActionPrecondition(resource_version="1", expected_state="ready"),
            ),
            "_pin_model",
        ),
        (
            ModelUnpinAction(
                operation="model.unpin",
                target_type="model",
                target_id=uuid.uuid4(),
                precondition=OpsActionPrecondition(resource_version="1", expected_state="ready"),
            ),
            "_unpin_model",
        ),
        (
            InstanceLoadAction(
                operation="instance.load",
                target_type="model",
                target_id=uuid.uuid4(),
                parameters=InstanceLoadParameters(variant_id=uuid.uuid4()),
                precondition=OpsActionPrecondition(resource_version="1", expected_state="ready"),
            ),
            "_load_instance",
        ),
    ],
)
async def test_each_confirmable_action_dispatches_only_to_its_fixed_handler(
    monkeypatch: pytest.MonkeyPatch, action: object, handler: str
) -> None:
    selected = AsyncMock(return_value={"dispatched": True})
    monkeypatch.setattr(f"coire_api.ops_actions.{handler}", selected)
    audit = AsyncMock()
    monkeypatch.setattr("coire_api.ops_actions.write_principal_audit", audit)
    principal = Principal(kind=PrincipalKind.ADMIN, user_id=uuid.uuid4())

    result = await execute_action(
        AsyncMock(),
        action,  # type: ignore[arg-type]
        principal,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )

    assert result == {"dispatched": True}
    selected.assert_awaited_once()
    audit.assert_awaited_once()
