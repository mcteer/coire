"""Historical intent and full-state identities survive preference contract changes."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from coire_core.models.training import parse_resolved_training_spec, parse_training_spec
from coire_core.models.training_node import CheckpointWorkerState, TrainingArtifactManifest

FIXTURES = Path(__file__).resolve().parents[4] / "tests/fixtures/preference/legacy_training"


@pytest.mark.parametrize(
    "name", ["recipe_v1", "recipe_v2", "resolved_v1", "resolved_v2", "checkpoint", "worker_state"]
)
def test_frozen_serialized_identity(name: str) -> None:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    value: BaseModel
    if name.startswith("recipe"):
        value = parse_training_spec(data)
    elif name.startswith("resolved"):
        value = parse_resolved_training_spec(data)
    elif name == "checkpoint":
        value = TrainingArtifactManifest.model_validate(data)
    else:
        value = CheckpointWorkerState.model_validate(data)
    actual = value.model_dump(mode="json")
    assert actual == data
    digest = hashlib.sha256(
        json.dumps(
            actual, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()
    assert digest == json.loads((FIXTURES / "digests.json").read_text())[name]
