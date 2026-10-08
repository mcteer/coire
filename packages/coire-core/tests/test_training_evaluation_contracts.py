"""V2 evaluation intent must never rewrite v1 recipe or checkpoint identities."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, ValidationError

from coire_core.models.training import (
    ResolvedTrainingSpec,
    TrainingSpec,
    TrainingSpecV2,
    parse_resolved_training_spec,
    parse_training_spec,
)
from coire_core.models.training_node import CheckpointWorkerState, TrainingArtifactManifest

FIXTURES = Path(__file__).resolve().parents[3] / "tests/fixtures/evaluations/legacy_training"


def load(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((FIXTURES / f"{name}.json").read_text()))


def test_frozen_v1_documents_and_full_checkpoint_are_unchanged() -> None:
    expected = load("digests")["digests"]
    documents: dict[str, BaseModel] = {
        "recipe": parse_training_spec(load("recipe")),
        "resolved": parse_resolved_training_spec(load("resolved")),
        "checkpoint": TrainingArtifactManifest.model_validate(load("checkpoint")),
        "worker_state": CheckpointWorkerState.model_validate(load("worker_state")),
    }
    assert isinstance(documents["recipe"], TrainingSpec)
    assert isinstance(documents["resolved"], ResolvedTrainingSpec)
    for name, value in documents.items():
        payload = value.model_dump(mode="json")
        digest = (
            value.canonical_sha256()
            if isinstance(value, (TrainingSpec, TrainingSpecV2, TrainingArtifactManifest))
            else hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest()
        )
        assert payload == load(name)
        assert digest == expected[name]
    assert "suites" not in documents["recipe"].model_dump()["eval"]


def v2() -> dict[str, Any]:
    data = load("recipe")
    data["schema_version"] = 2
    data["output"]["checkpoint_every_updates"] = 8
    data["eval"]["suites"] = [
        {"suite_id": "task-coding-instructions", "suite_version": 1, "checkpoint_updates": [8, 16]}
    ]
    return data


def test_v2_requires_explicit_suites_and_recoverable_pre_final_boundaries() -> None:
    assert isinstance(parse_training_spec(v2()), TrainingSpecV2)
    for updates in ([7], [32], [16, 8], [8, 8], [0]):
        data = v2()
        data["eval"]["suites"][0]["checkpoint_updates"] = updates
        with pytest.raises(ValidationError):
            parse_training_spec(data)
    suite_options: tuple[list[dict[str, Any]], ...] = ([], v2()["eval"]["suites"] * 2)
    for suites in suite_options:
        data = v2()
        data["eval"]["suites"] = suites
        with pytest.raises(ValidationError):
            parse_training_spec(data)


def test_legacy_recipe_without_version_remains_v1_and_rejects_new_fields() -> None:
    data = load("recipe")
    data.pop("schema_version")
    assert isinstance(parse_training_spec(data), TrainingSpec)
    data["eval"]["suites"] = []
    with pytest.raises(ValidationError):
        parse_training_spec(data)


def test_v2_resolution_requires_frozen_base_evaluation_runtime_and_exact_schedule() -> None:
    from coire_core.models.evaluation import EvaluationWorkload

    work = EvaluationWorkload.model_validate_json((FIXTURES.parent / "workload.json").read_bytes())
    resolved = load("resolved")
    resolved["spec"] = v2()
    schedule = resolved["spec"]["eval"]["suites"][0]
    suite = work.suite.model_copy(
        update={"suite_id": schedule["suite_id"], "version": schedule["suite_version"]}
    )
    resolved["evaluations"] = [{"schedule": schedule, "suite": suite.model_dump(mode="json")}]
    with pytest.raises(ValidationError):
        parse_resolved_training_spec(resolved)
    base = work.target.model_dump(mode="json")
    base["target"]["model_id"] = resolved["spec"]["model"]["model_id"]
    base["target"]["variant_id"] = resolved["spec"]["model"]["variant_id"]
    base["target"]["base_manifest_sha256"] = resolved["base_manifest_sha256"]
    base["public_selector"] = base["target"]["model_id"]
    base["runtime"]["tokenizer_sha256"] = resolved["tokenizer_sha256"]
    base["runtime"]["template_sha256"] = resolved["template_sha256"]
    resolved["evaluation_base"] = base
    value = parse_resolved_training_spec(resolved)
    assert value.model_dump(mode="json")["evaluation_base"] == base
    base["target"]["variant_id"] = "10000000-0000-4000-8000-000000000099"
    with pytest.raises(ValidationError):
        parse_resolved_training_spec(resolved)


def test_version_union_keeps_numeric_constants_in_generated_api_contract() -> None:
    from pydantic import TypeAdapter

    from coire_core.models.training import TrainingSpecDocument

    schema = TypeAdapter(TrainingSpecDocument).json_schema()
    # OpenAPI discriminator mappings are strings; consumers must use numeric oneOf constants.
    assert "discriminator" not in schema
    assert schema["$defs"]["TrainingSpec"]["properties"]["schema_version"]["const"] == 1
    assert schema["$defs"]["TrainingSpecV2"]["properties"]["schema_version"]["const"] == 2
