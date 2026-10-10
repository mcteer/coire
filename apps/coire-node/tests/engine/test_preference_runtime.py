"""Exact independent initial reference and RNG preservation on acquired tiny bases."""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

import pytest

from coire_core.models.adapters import InferenceTarget
from coire_core.models.training import TrainingParameterization

pytestmark = pytest.mark.engine


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
def test_initial_runtime_reference_matches_policy_without_changing_policy_rng(
    training_model: Path,
    training_kind: Literal["lora", "qlora", "dora"],
    objective: Literal["dpo", "orpo"],
) -> None:
    import mlx.core as mx
    from mlx.utils import tree_flatten

    from coire_node.training.checkpoints import capture_mlx_rng_key
    from coire_node.training.objectives import load_sft_runtime
    from coire_node.training.preference_runtime import (
        load_preference_runtime,
        validate_preference_input,
    )

    parameters = TrainingParameterization(
        kind=training_kind, rank=2, target_modules=["self_attn.q_proj", "self_attn.v_proj"]
    )
    source = validate_preference_input(training_model, parameters)
    target = InferenceTarget(
        model_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        base_manifest_sha256=source.manifest.sha256(),
    )
    baseline = load_sft_runtime(source, seed=42)
    mx.eval(baseline.parameters())
    expected_rng = capture_mlx_rng_key()
    del baseline
    runtime = load_preference_runtime(source, seed=42, objective=objective, initial_target=target)
    assert capture_mlx_rng_key() == expected_rng
    assert runtime.policy.trainable_keys
    if objective == "orpo":
        assert runtime.reference is None
        return
    assert runtime.reference is not None and runtime.reference.model is not runtime.policy.model
    assert not tree_flatten(
        cast(Callable[[], dict[str, object]], runtime.reference.model.trainable_parameters)()
    )
    tokens = mx.array([[1, 2, 3]], dtype=mx.int32)
    policy = cast(Callable[[mx.array], mx.array], runtime.policy.model)
    reference = cast(Callable[[mx.array], mx.array], runtime.reference.model)
    before = reference(tokens)
    policy_before = policy(tokens)
    mx.eval(before, policy_before)
    assert bool(mx.array_equal(before, policy_before))
    weights = dict(runtime.reference.parameters())
    current = runtime.policy.parameters()
    key = next(iter(runtime.policy.trainable_keys))
    runtime.policy.model.load_weights([(key, current[key] + 0.01)], strict=False)
    after = reference(tokens)
    mx.eval(before, after, runtime.reference.parameters())
    assert bool(mx.array_equal(before, after))
    assert all(
        bool(mx.array_equal(weights[key], runtime.reference.parameters()[key])) for key in weights
    )


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
def test_chained_initialization_loads_complete_parent_in_policy_and_reference(
    training_model: Path,
    training_kind: Literal["lora", "qlora", "dora"],
    tmp_path: Path,
    objective: Literal["dpo", "orpo"],
) -> None:
    import hashlib
    import json

    import mlx.core as mx
    from mlx.utils import tree_flatten

    from coire_core.models.training_node import TrainingArtifactFile, TrainingArtifactManifest
    from coire_node.training.objectives import load_sft_runtime
    from coire_node.training.preference_runtime import (
        load_preference_runtime,
        validate_preference_input,
    )

    parameters = TrainingParameterization(
        kind=training_kind, rank=2, target_modules=["self_attn.q_proj", "self_attn.v_proj"]
    )
    source = validate_preference_input(training_model, parameters)
    base = InferenceTarget(
        model_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        base_manifest_sha256=source.manifest.sha256(),
    )
    parent = load_sft_runtime(source, seed=42)
    values = {
        key: value + 0.01
        for key, value in parent.parameters().items()
        if key in parent.trainable_keys
    }
    mx.eval(values)
    identity = uuid.uuid4()
    directory = tmp_path / str(identity)
    directory.mkdir(mode=0o700)
    weights = directory / "adapters.safetensors"
    mx.save_safetensors(str(weights), values)
    weights.chmod(0o600)
    config = directory / "adapter_config.json"
    config.write_text(
        json.dumps(
            {
                "fine_tune_type": "lora",
                "num_layers": parameters.num_layers,
                "lora_parameters": {
                    "rank": parameters.rank,
                    "scale": parameters.scale,
                    "dropout": 0.0,
                    "keys": parameters.target_modules,
                },
                "coire_base_model_id": str(base.model_id),
                "coire_base_variant_id": str(base.variant_id),
                "coire_base_manifest_sha256": base.base_manifest_sha256,
            }
        )
    )
    config.chmod(0o600)
    manifest = TrainingArtifactManifest(
        artifact_id=identity,
        kind="adapter",
        files=[
            TrainingArtifactFile(
                id="adapter",
                name=weights.name,
                bytes=weights.stat().st_size,
                sha256=hashlib.sha256(weights.read_bytes()).hexdigest(),
            ),
            TrainingArtifactFile(
                id="config",
                name=config.name,
                bytes=config.stat().st_size,
                sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
            ),
        ],
        total_bytes=weights.stat().st_size + config.stat().st_size,
    )
    path = directory / "manifest.json"
    path.write_text(manifest.model_dump_json())
    path.chmod(0o600)
    target = base.model_copy(
        update={"adapter_id": identity, "adapter_manifest_sha256": manifest.canonical_sha256()}
    )
    del parent
    runtime = load_preference_runtime(
        source, seed=99, objective=objective, initial_target=target, initial_adapter=directory
    )
    mx.eval(runtime.policy.parameters())
    assert all(
        bool(mx.array_equal(value, runtime.policy.parameters()[key]))
        for key, value in values.items()
    )
    if objective == "dpo":
        assert runtime.reference is not None
        mx.eval(runtime.reference.parameters())
        assert all(
            bool(mx.array_equal(value, runtime.reference.parameters()[key]))
            for key, value in values.items()
        )
        assert not tree_flatten(
            cast(Callable[[], dict[str, object]], runtime.reference.model.trainable_parameters)()
        )
    else:
        assert runtime.reference is None
