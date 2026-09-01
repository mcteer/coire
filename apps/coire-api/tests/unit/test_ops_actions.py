from __future__ import annotations

from coire_api.ops_actions import ACTION_REGISTRY
from coire_core.models.ops import (
    InstanceLoadAction,
    InstanceUnloadAction,
    ModelPinAction,
    ModelUnpinAction,
    RunKillAction,
)


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
