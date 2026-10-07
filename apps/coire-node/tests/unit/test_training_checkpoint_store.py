"""Real safetensors with synthetic CPU arrays verify checkpoint integrity and state trees."""

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from safetensors.numpy import load_file, save_file

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.datasets import TokenizedTrainingExample
from coire_core.models.training import TrainingOptimizer
from coire_core.models.training_node import CheckpointWorkerState
from coire_node.training.checkpoints import CheckpointStore
from coire_node.training.sampler import SingleSourceSampler

JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


class NumpyTensorIO:
    def is_tensor(self, value: object) -> bool:
        return isinstance(value, np.ndarray)

    def shape(self, value: Any) -> list[int]:
        return list(value.shape)

    def dtype(self, value: Any) -> str:
        return str(value.dtype)

    def save(self, path: Path, values: dict[str, Any]) -> None:
        save_file(values, str(path))

    def load(self, path: Path) -> dict[str, Any]:
        return dict(load_file(str(path)))


def state() -> CheckpointWorkerState:
    examples = [
        TokenizedTrainingExample(
            source_row=i + 1,
            content_sha256="a" * 64,
            tokens=[1, 2, 3],
            target_mask=[False, False, True],
            target_start=2,
        )
        for i in range(4)
    ]
    sampler = SingleSourceSampler(
        examples, dataset_sha256="b" * 64, batch_size=2, seed=0, max_sequence_length=8
    )
    sampler.next_batch()
    return CheckpointWorkerState(
        job_id=JOB,
        attempt_id=JOB,
        fence=1,
        completed_update=3,
        rank=0,
        world_size=1,
        runtime_sha256="c" * 64,
        resolved_spec_sha256="d" * 64,
        optimizer=TrainingOptimizer(updates=32),
        mlx_rng_key=(123, 456),
        sampler=sampler.snapshot(),
    )


def test_full_optimizer_tree_rng_sampler_and_tensor_state_roundtrip(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints", tensor_io=NumpyTensorIO(), disk_floor_bytes=0)
    adapter = {"layers.0.self_attn.q_proj.lora_a": np.array([[0.1, 0.2]], dtype=np.float32)}
    optimizer: dict[str, Any] = {
        "step": np.array(3, dtype=np.uint32),
        "moments": [
            {
                "m": np.array([0.5, 0.75], dtype=np.float32),
                "v": np.array([1.0, 2.0], dtype=np.float32),
            }
        ],
        "label": (None, "constant"),
    }
    manifest = store.save(state(), adapter, optimizer)
    restored = store.restore(
        manifest.artifact_id,
        expected_runtime_sha256="c" * 64,
        expected_resolved_spec_sha256="d" * 64,
    )
    assert restored.state.mlx_rng_key == (123, 456)
    assert restored.state.sampler == state().sampler
    np.testing.assert_array_equal(
        restored.adapter_tensors[next(iter(adapter))], next(iter(adapter.values()))
    )
    np.testing.assert_array_equal(
        restored.optimizer_state["moments"][0]["v"], optimizer["moments"][0]["v"]
    )
    assert restored.optimizer_state["step"].item() == 3
    assert restored.optimizer_state["label"] == (None, "constant")


def test_corrupt_files_and_changed_runtime_cannot_resume(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints", tensor_io=NumpyTensorIO(), disk_floor_bytes=0)
    manifest = store.save(
        state(),
        {"lora_a": np.ones((2, 2), dtype=np.float32)},
        {"step": np.array(3, dtype=np.uint32)},
    )
    with pytest.raises(TrainingConflict):
        store.restore(
            manifest.artifact_id,
            expected_runtime_sha256="f" * 64,
            expected_resolved_spec_sha256="d" * 64,
        )
    (store.path_for(manifest.artifact_id) / "rank-0-optimizer.safetensors").write_bytes(b"corrupt")
    with pytest.raises(TrainingValidationError):
        store.restore(
            manifest.artifact_id,
            expected_runtime_sha256="c" * 64,
            expected_resolved_spec_sha256="d" * 64,
        )


def test_artifacts_are_not_overwritten_and_last_complete_point_is_protected(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints", tensor_io=NumpyTensorIO(), disk_floor_bytes=0)
    adapter = {"lora_a": np.ones((2, 2), dtype=np.float32)}
    optimizer = {"step": np.array(3, dtype=np.uint32)}
    first = store.save(state(), adapter, optimizer)
    with pytest.raises(TrainingConflict):
        store.save(state(), adapter, optimizer, artifact_id=first.artifact_id)
    second = store.save(
        state().model_copy(update={"completed_update": 4}),
        adapter,
        {"step": np.array(4, dtype=np.uint32)},
    )
    removed = store.prune(
        committed_ids=[first.artifact_id, second.artifact_id],
        protected_ids={first.artifact_id},
        keep_last=1,
    )
    assert removed == []
    assert (
        store.path_for(first.artifact_id).is_dir() and store.path_for(second.artifact_id).is_dir()
    )


def test_corrupt_newest_checkpoint_falls_back_and_does_not_evict_previous(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints", tensor_io=NumpyTensorIO(), disk_floor_bytes=0)
    adapter = {"lora_a": np.ones((2, 2), dtype=np.float32)}
    optimizer = {"step": np.array(3, dtype=np.uint32)}
    first = store.save(state(), adapter, optimizer)
    second = store.save(
        state().model_copy(update={"completed_update": 4}),
        adapter,
        {"step": np.array(4, dtype=np.uint32)},
    )
    (store.path_for(second.artifact_id) / "rank-0-state.json").write_bytes(b"corrupt")
    recovered = store.newest_valid(
        [first.artifact_id, second.artifact_id],
        expected_runtime_sha256="c" * 64,
        expected_resolved_spec_sha256="d" * 64,
    )
    assert recovered.manifest.artifact_id == first.artifact_id
    assert (
        store.prune(
            committed_ids=[first.artifact_id, second.artifact_id], protected_ids=set(), keep_last=1
        )
        == []
    )
