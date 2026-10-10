"""Preference initialization metadata refuses unsupported input before native imports."""

from pathlib import Path

import pytest
from test_training_loader import make_source, parameters

from coire_core.errors import TrainingValidationError
from coire_core.models.adapters import InferenceTarget
from coire_node.training.preference_runtime import (
    load_preference_runtime,
    validate_preference_input,
)


def test_preference_input_accepts_only_zero_dropout_lora_qlora(tmp_path: Path) -> None:
    root = make_source(tmp_path, {"model_type": "qwen2", "num_hidden_layers": 2})
    assert validate_preference_input(root, parameters()).parameterization.kind == "lora"
    for value in (parameters("dora"), parameters().model_copy(update={"dropout": 0.1})):
        with pytest.raises(TrainingValidationError):
            validate_preference_input(root, value)


def test_preference_load_refuses_core_before_native_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uuid

    root = make_source(tmp_path, {"model_type": "qwen2", "num_hidden_layers": 2})
    source = validate_preference_input(root, parameters())
    target = InferenceTarget(
        model_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        base_manifest_sha256=source.manifest.sha256(),
    )
    monkeypatch.setattr(
        "coire_node.training.preference_runtime.platform.node", lambda: "coire-core.lab"
    )
    with pytest.raises(TrainingValidationError, match="core"):
        load_preference_runtime(source, seed=42, objective="dpo", initial_target=target)
