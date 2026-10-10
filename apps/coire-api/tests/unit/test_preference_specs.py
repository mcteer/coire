"""Preference identities are explicit, bounded and distinct from frozen SFT keys."""

import hashlib
import json
import uuid

import pytest
from test_training_specs import SOURCE

from coire_api.training.specs import parse_spec, parse_submission, training_config_digest
from coire_core.errors import TrainingValidationError
from coire_core.models.preference import DpoOptions
from coire_core.models.training import TrainingSpecV3, TrainingSubmission


def recipe(objective: str = "dpo") -> str:
    options = "beta: 0.1" if objective == "dpo" else "weight: 0.1"
    return (
        f"schema_version: 3\nobjective: {objective}\nobjective_options: {{{options}}}\ninit_adapter: null\n"
        + SOURCE
    )


def test_explicit_v3_recipe_roundtrip_and_no_sft_digest_alias() -> None:
    parsed = parse_submission(TrainingSubmission(source_yaml=recipe()))
    assert isinstance(parsed.spec, TrainingSpecV3) and parsed.spec.init_adapter is None
    assert (
        parsed.intent_sha256
        != parse_submission(TrainingSubmission(source_yaml=SOURCE)).intent_sha256
    )
    assert training_config_digest(parsed.spec) != training_config_digest(parse_spec(SOURCE))
    assert training_config_digest(parsed.spec) != training_config_digest(parse_spec(recipe("orpo")))
    config = parsed.spec.model_dump(mode="json")
    config["output"].pop("adapter_slug")
    config["eval"].pop("suites")
    expected = hashlib.sha256(
        json.dumps(
            ["coire-preference-profile-v1", config],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    assert training_config_digest(parsed.spec) == expected
    changed_slug = parsed.spec.model_copy(deep=True)
    changed_slug.output.adapter_slug = "other-output"
    assert training_config_digest(changed_slug) == training_config_digest(parsed.spec)
    parent = parsed.spec.model_copy(update={"init_adapter": uuid.uuid4()})
    assert training_config_digest(parent) != training_config_digest(parsed.spec)
    changed_options = parsed.spec.model_copy(deep=True)
    assert isinstance(changed_options.objective_options, DpoOptions)
    changed_options.objective_options.beta = 0.2
    assert training_config_digest(changed_options) != training_config_digest(parsed.spec)


@pytest.mark.parametrize(
    "change", ["omit_init", "omit_version", "options", "dora", "dropout", "distributed"]
)
def test_preference_recipe_refuses_implicit_or_unqualified_inputs(change: str) -> None:
    source = recipe()
    if change == "omit_init":
        source = source.replace("init_adapter: null\n", "")
    elif change == "omit_version":
        source = source.replace("schema_version: 3\n", "")
    elif change == "options":
        source = source.replace("beta: 0.1", "weight: 0.1")
    elif change == "dora":
        source = source.replace("kind: lora", "kind: dora")
    elif change == "dropout":
        source = source.replace("kind: lora", "kind: lora\n  dropout: 0.1")
    else:
        source += "placement: {mode: data_parallel}\n"
    with pytest.raises(TrainingValidationError):
        parse_spec(source)


@pytest.mark.parametrize("versions", [[1], [1, 2], [1, 2, 3]])
async def test_preference_preflight_requires_v3_on_both_studios(
    monkeypatch: pytest.MonkeyPatch, versions: list[int]
) -> None:
    from types import SimpleNamespace

    from coire_api.nodes_client import NodeClient
    from coire_api.training.preference_specs import require_preference_capabilities
    from coire_core.errors import TrainingUnavailable
    from coire_core.settings import Settings

    seen: list[str] = []

    async def health(self: NodeClient, node: str) -> object:
        seen.append(node)
        return SimpleNamespace(
            training_capabilities=SimpleNamespace(
                spec_versions=[1, 2, 3] if node == "coire-edge-a" else versions,
                evaluation_checkpoint_ack_versions=[1],
            )
        )

    monkeypatch.setattr(NodeClient, "health", health)
    if 3 not in versions:
        with pytest.raises(TrainingUnavailable):
            await require_preference_capabilities(Settings())
    else:
        await require_preference_capabilities(Settings())
    assert seen == ["coire-edge-a", "coire-edge-b"]


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
def test_shipped_preference_recipes_bind_registry_ids_and_roundtrip(objective: str) -> None:
    from string import Template

    from coire_api.training.specs import parse_spec, training_recipes
    from coire_core.models.training import TrainingSpecV3

    recipe = next(item for item in training_recipes().items if item.id == objective)
    bound = Template(recipe.template_yaml).substitute(
        model_id="00000000-0000-4000-8000-000000000001",
        variant_id="00000000-0000-4000-8000-000000000002",
        dataset_id="00000000-0000-4000-8000-000000000003",
        adapter_slug="preference-test",
    )
    spec = parse_spec(bound)
    assert isinstance(spec, TrainingSpecV3) and spec.objective == objective
    assert spec.init_adapter is None and spec.parameterization.dropout == 0
    assert parse_spec(spec.model_dump_json()) == spec
