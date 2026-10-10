"""V3 full-state artifacts retain objective/initial identity independently of SFT."""

import uuid
from pathlib import Path

import numpy as np
import pytest
from test_preference_sampler import sampler, source
from test_training_checkpoint_store import NumpyTensorIO
from test_training_checkpoint_store import state as sft_state

from coire_core.models.adapters import InferenceTarget
from coire_core.models.preference import DpoOptions, OrpoOptions
from coire_core.models.training_node import CheckpointWorkerStateV3
from coire_node.training.checkpoints import CheckpointStore


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
def test_v3_checkpoint_roundtrips_exact_pair_cursor_and_reference(
    tmp_path: Path, objective: str
) -> None:
    pairs = sampler([source()])
    pairs.next_batch()
    initial = InferenceTarget(
        model_id=uuid.uuid4(), variant_id=uuid.uuid4(), base_manifest_sha256="a" * 64
    )
    state = CheckpointWorkerStateV3.model_validate(
        {
            **sft_state().model_dump(mode="json"),
            "schema_version": 3,
            "optimizer": {**sft_state().optimizer.model_dump(mode="json"), "batch_size": 2},
            "objective": objective,
            "objective_options": (
                DpoOptions(beta=0.1) if objective == "dpo" else OrpoOptions(weight=0.1)
            ).model_dump(),
            "initial_target": initial.model_dump(mode="json"),
            "reference_target": initial.model_dump(mode="json") if objective == "dpo" else None,
            "sampler": pairs.snapshot().model_dump(mode="json"),
        }
    )
    store = CheckpointStore(tmp_path / "artifacts", tensor_io=NumpyTensorIO(), disk_floor_bytes=0)
    adapter = {"layer.lora_a": np.array([[0.1, 0.2]], dtype=np.float32)}
    optimizer = {
        "step": np.array(state.completed_update, dtype=np.uint32),
        "moments": {"m": np.array([0.5], dtype=np.float32)},
    }
    manifest = store.save(state, adapter, optimizer)
    restored = store.restore(
        manifest.artifact_id,
        expected_runtime_sha256=state.runtime_sha256,
        expected_resolved_spec_sha256=state.resolved_spec_sha256,
    )
    assert isinstance(restored.state, CheckpointWorkerStateV3)
    assert restored.state.initial_target == initial
    assert restored.state.reference_target == state.reference_target
    assert restored.state.objective_options == state.objective_options
    assert restored.state.mlx_rng_key == state.mlx_rng_key
    restarted = sampler([source()])
    restarted.restore(restored.state.sampler)
    assert pairs.next_batch() == restarted.next_batch()
    np.testing.assert_array_equal(restored.adapter_tensors["layer.lora_a"], adapter["layer.lora_a"])
