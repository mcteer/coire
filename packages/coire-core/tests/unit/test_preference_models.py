"""Preference examples and new intent refuse ambiguities while preserving SFT."""

import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from coire_core.models.preference import PreferenceRow
from coire_core.models.training import TrainingSpecV3, parse_training_spec

FIXTURE = (
    Path(__file__).resolve().parents[4] / "tests/fixtures/preference/legacy_training/recipe_v1.json"
)


def recipe(objective: str = "dpo") -> dict[str, Any]:
    data = json.loads(FIXTURE.read_text())
    data.update(
        schema_version=3,
        objective=objective,
        objective_options={"beta": 0.1} if objective == "dpo" else {"weight": 0.1},
        init_adapter=None,
    )
    return cast(dict[str, Any], data)


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
def test_v3_is_explicit_and_suites_can_be_empty(objective: str) -> None:
    value = parse_training_spec(recipe(objective))
    assert isinstance(value, TrainingSpecV3)
    assert value.eval.suites == []
    assert value.init_adapter is None
    assert parse_training_spec(value.model_dump(mode="json")) == value


@pytest.mark.parametrize(
    "mutation", ["wrong_options", "nonfinite", "dropout", "dora", "distributed"]
)
def test_v3_refuses_unqualified_configs(mutation: str) -> None:
    data = recipe()
    if mutation == "wrong_options":
        data["objective_options"] = {"weight": 0.1}
    elif mutation == "nonfinite":
        data["objective_options"] = {"beta": float("nan")}
    elif mutation == "dropout":
        data["parameterization"]["dropout"] = 0.1
    elif mutation == "dora":
        data["parameterization"]["kind"] = "dora"
    else:
        data["placement"]["mode"] = "data_parallel"
    with pytest.raises(ValidationError):
        parse_training_spec(data)


@pytest.mark.parametrize(
    "chosen,rejected", [("", "no"), ("yes", "yes"), (" ", "no"), ("x" * 65537, "no")]
)
def test_response_bounds_and_nontriviality(chosen: str, rejected: str) -> None:
    with pytest.raises(ValidationError):
        PreferenceRow.model_validate(
            {"prompt": [{"role": "user", "content": "hi"}], "chosen": chosen, "rejected": rejected}
        )


@pytest.mark.parametrize(
    "prompt",
    [
        [{"role": "assistant", "content": "hi"}],
        [{"role": "system", "content": "hi"}],
        [{"role": "user", "content": "hi", "tools": []}],
    ],
)
def test_prompt_is_inert_text_with_a_final_user(prompt: list[dict[str, object]]) -> None:
    with pytest.raises(ValidationError):
        PreferenceRow.model_validate({"prompt": prompt, "chosen": "yes", "rejected": "no"})


def test_pair_hash_preserves_orientation_but_group_hash_does_not() -> None:
    first = PreferenceRow.model_validate(
        {"prompt": [{"role": "user", "content": "hi"}], "chosen": "yes", "rejected": "no"}
    )
    reverse = PreferenceRow(prompt=first.prompt, chosen="no", rejected="yes")
    assert first.prompt_sha256() == reverse.prompt_sha256()
    assert first.content_sha256() != reverse.content_sha256()
