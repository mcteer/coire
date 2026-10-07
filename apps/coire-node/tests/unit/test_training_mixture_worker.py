"""Frozen mixture compilation and real synthetic CPU checkpoint continuation; no MLX."""

import hashlib
import json
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from safetensors.numpy import load_file, save_file

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.datasets import (
    DatasetAnalysis,
    DatasetAnalysisBinding,
    DatasetFormat,
    SplitManifest,
    TokenDistribution,
)
from coire_core.models.training_node import CheckpointWorkerState, TrainingPrepareRequest
from coire_core.training_data import normalize_row, split_digest
from coire_node.training.checkpoints import CheckpointStore
from coire_node.training.journal import TrainingJournal
from coire_node.training.sampler import MixtureSampler, SingleSourceSampler
from coire_node.training.supervisor import TrainingSupervisor
from coire_node.training.worker import (
    FrozenInputs,
    compile_samples,
    load_all_frozen_inputs,
    payload_sha256,
    resolved_digest,
    validate_frozen_inputs,
)

JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


@pytest.mark.parametrize("count", [1, 2])
def test_compiler_reports_the_buffer_reservation_required_by_a_measured_replay(
    tmp_path: Path, count: int
) -> None:
    prepared, inputs = frozen_sources(tmp_path, count=count)
    costs: list[int] = []
    compile_samples(prepared, inputs, SyntheticTokenizer(), buffer_observer=costs.append)
    assert costs and costs == sorted(costs) and costs[-1] > 0
    prepared.resolved.resource_envelope.buffer_bytes = costs[-1]
    compile_samples(prepared, inputs, SyntheticTokenizer())
    prepared.resolved.resource_envelope.buffer_bytes -= 1
    with pytest.raises(TrainingValidationError, match="buffer envelope"):
        compile_samples(prepared, inputs, SyntheticTokenizer())


class CpuTensorIO:
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


class SyntheticTokenizer:
    """A synthetic CPU fixture, not tokenization evidence for a registry model."""

    has_chat_template = False

    def encode(self, text: str) -> list[int]:
        return [1, int(text)]

    def apply_chat_template(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        tokenize: bool,
        add_generation_prompt: bool,
        enable_thinking: bool,
    ) -> list[int]:
        raise AssertionError("Raw synthetic text must not invoke a template")


def frozen_sources(
    tmp_path: Path, count: int = 2
) -> tuple[TrainingPrepareRequest, list[FrozenInputs]]:
    ids = [uuid.UUID(int=index + 1) for index in range(count)]
    command = TrainingPrepareRequest.model_validate(
        {
            "command_id": str(uuid.uuid4()),
            "job_id": JOB,
            "attempt_id": JOB,
            "fence": 1,
            "request_sha256": "a" * 64,
            "node": "coire-edge-a",
            "rank": 0,
            "world_size": 1,
            "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=29)).isoformat(),
            "reservation_id": str(uuid.uuid4()),
            "disk_reservation_id": str(uuid.uuid4()),
            "resolved": {
                "spec": {
                    "model": {
                        "model_id": str(uuid.UUID(int=50)),
                        "variant_id": str(uuid.UUID(int=51)),
                    },
                    "data": {
                        "train": {
                            "datasets": [
                                {
                                    "dataset_id": str(key),
                                    "sample_count": 4,
                                    "mixture_proportion": 1 / count,
                                }
                                for key in ids
                            ],
                            "epoch_samples": 6 if count == 2 else 4,
                            "seed": 0,
                        },
                        "validation": {"dataset_ids": [str(key) for key in ids], "seed": 7},
                        "loss_policy": "all_tokens",
                    },
                    "parameterization": {"rank": 2, "target_modules": ["self_attn.q_proj"]},
                    "optim": {"updates": 40, "batch_size": 2, "max_sequence_length": 8},
                    "output": {"adapter_slug": "synthetic-mixture"},
                },
                "base_manifest_sha256": "b" * 64,
                "datasets": [
                    {
                        "dataset_id": str(key),
                        "analysis_id": str(uuid.uuid4()),
                        "source_sha256": "c" * 64,
                        "split_sha256": "d" * 64,
                        "analysis_sha256": "e" * 64,
                    }
                    for key in ids
                ],
                "tokenizer_sha256": "f" * 64,
                "template_sha256": "a" * 64,
                "enable_thinking": False,
                "runtime_sha256": "b" * 64,
                "worker_version": "1",
                "resource_envelope": {
                    "weight_bytes": 1,
                    "adapter_bytes": 1,
                    "optimizer_bytes": 1,
                    "activation_bytes": 1,
                    "buffer_bytes": 1024**2,
                    "safety_bytes": 1,
                    "checkpoint_bytes": 1024**2,
                    "evidence_sha256": "c" * 64,
                },
            },
        }
    )
    inputs = []
    for selected in command.resolved.datasets:
        rows = [{"text": str(selected.dataset_id.int * 100 + row)} for row in range(1, 7)]
        path = tmp_path / f"source-{selected.dataset_id}.jsonl"
        path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
        path.chmod(0o600)
        selected.source_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        split = SplitManifest(
            dataset_id=selected.dataset_id,
            source_sha256=selected.source_sha256,
            seed=0,
            train_rows=[1, 2, 3, 4],
            validation_rows=[5, 6],
            row_content_sha256=[
                normalize_row(
                    row,
                    format=DatasetFormat.TEXT,
                    dataset_id=selected.dataset_id,
                    source_row=index + 1,
                ).content_sha256()
                for index, row in enumerate(rows)
            ],
        )
        selected.split_sha256 = split_digest(split)
        binding = DatasetAnalysisBinding(
            dataset_id=selected.dataset_id,
            model_id=command.resolved.spec.model.model_id,
            variant_id=command.resolved.spec.model.variant_id,
            base_manifest_sha256=command.resolved.base_manifest_sha256,
            source_sha256=selected.source_sha256,
            split_sha256=selected.split_sha256,
            format=DatasetFormat.TEXT,
            model_slug="synthetic-base",
        )
        analysis = DatasetAnalysis(
            id=selected.analysis_id,
            dataset_id=selected.dataset_id,
            model_id=binding.model_id,
            variant_id=binding.variant_id,
            state="succeeded",
            row_count=6,
            created_at=datetime.now(UTC),
            tokenizer_sha256=command.resolved.tokenizer_sha256,
            template_sha256=command.resolved.template_sha256,
            runtime_sha256=command.resolved.runtime_sha256,
            tokens=TokenDistribution(
                minimum=2, maximum=2, p50=2, p95=2, histogram=[6], upper_bounds=[2]
            ),
        )
        selected.analysis_sha256 = payload_sha256(analysis)
        inputs.append(FrozenInputs(binding, split, analysis, path))
    return command, inputs


def test_canonical_split_digest_only_and_legacy_single_order_is_preserved(tmp_path: Path) -> None:
    command, inputs = frozen_sources(tmp_path, 1)
    train, validation = compile_samples(command, inputs, SyntheticTokenizer())
    assert isinstance(train, SingleSourceSampler) and isinstance(validation, SingleSourceSampler)
    expected = SingleSourceSampler(
        train.dataset,
        dataset_sha256=train.dataset_sha256,
        batch_size=2,
        seed=0,
        max_sequence_length=8,
    )
    assert [train.next_batch() for _ in range(12)] == [expected.next_batch() for _ in range(12)]
    wrong = hashlib.sha256(inputs[0].split.model_dump_json().encode()).hexdigest()
    assert wrong != command.resolved.datasets[0].split_sha256
    command.resolved.datasets[0].split_sha256 = wrong
    binding = inputs[0].binding.model_copy(update={"split_sha256": wrong})
    with pytest.raises(TrainingConflict, match="binding"):
        validate_frozen_inputs(command, replace(inputs[0], binding=binding))


@pytest.mark.asyncio
async def test_multi_source_private_journal_staging_and_compile(tmp_path: Path) -> None:
    command, inputs = frozen_sources(tmp_path)
    journal = TrainingJournal(
        tmp_path / "journal", node=command.node, admission_lock=threading.RLock()
    )
    supervisor = TrainingSupervisor(
        journal,
        interpreter=Path("/opt/coire/envs/v1/bin/python"),
        validate_ready=lambda _: None,
    )
    try:
        journal.prepare(
            command, memory_available=16 * 1024**2, disk_available=32 * 1024**2, disk_floor=0
        )
        for source in inputs:
            await supervisor.bind_inputs(command, source, multi=True, disk_available=32 * 1024**2)
        staged = load_all_frozen_inputs(command, supervisor.directory(command.attempt_id), journal)
        train, validation = compile_samples(command, staged, SyntheticTokenizer())
        assert isinstance(train, MixtureSampler) and isinstance(validation, MixtureSampler)
        sampled = [row[1] for _ in range(6) for row in train.next_batch().tokens]
        assert all(row % 100 <= 4 for row in sampled)
        assert sum(row // 100 == 1 for row in sampled) == 6
        held_out = [validation.next_batch().tokens[0][1] for _ in range(4)]
        assert set(held_out) == {105, 106, 205, 206}
        assert journal.get(command.attempt_id).get("input_files") is None
        inputs[0].source.write_bytes(b"changed")
        # Only node-owned staged bytes are consumed, never the original caller path.
        assert load_all_frozen_inputs(command, supervisor.directory(command.attempt_id), journal)
    finally:
        await supervisor.aclose()


def test_mixture_aggregate_cache_budget_missing_revision_and_byte_drift_are_refused(
    tmp_path: Path,
) -> None:
    command, inputs = frozen_sources(tmp_path)
    with pytest.raises(TrainingConflict, match="exact resolved"):
        compile_samples(command, inputs[:1], SyntheticTokenizer())
    command.resolved.resource_envelope.buffer_bytes = 1000
    with pytest.raises(TrainingValidationError, match="indices"):
        compile_samples(command, inputs, SyntheticTokenizer())
    command.resolved.resource_envelope.buffer_bytes = 40_000
    with pytest.raises(TrainingValidationError, match="cache"):
        compile_samples(command, inputs, SyntheticTokenizer())
    command.resolved.resource_envelope.buffer_bytes = 1024**2
    inputs[1].source.write_bytes(b'{"text":"999"}\n')
    with pytest.raises(TrainingConflict, match="digest"):
        compile_samples(command, inputs, SyntheticTokenizer())


def test_validation_only_revision_is_loaded_with_independent_sampler(tmp_path: Path) -> None:
    command, inputs = frozen_sources(tmp_path, 3)
    command.resolved.spec.data.train.datasets = command.resolved.spec.data.train.datasets[:2]
    for source in command.resolved.spec.data.train.datasets:
        source.mixture_proportion = 0.5
    command.resolved.spec.data.train.epoch_samples = 6
    command.resolved.spec.data.validation.dataset_ids = [inputs[2].binding.dataset_id]
    train, validation = compile_samples(command, inputs, SyntheticTokenizer())
    assert isinstance(train, MixtureSampler) and isinstance(validation, MixtureSampler)
    assert {key for key, _ in validation.last_batch_references} == set()
    saved = train.snapshot()
    assert {validation.next_batch().tokens[0][1] for _ in range(2)} == {305, 306}
    assert train.snapshot() == saved
    assert all(row[1] // 100 in (1, 2) for _ in range(3) for row in train.next_batch().tokens)


def test_frozen_mixture_leakage_is_refused_before_token_cache_work(tmp_path: Path) -> None:
    command, inputs = frozen_sources(tmp_path)
    hashes = list(inputs[1].split.row_content_sha256)
    hashes[4] = inputs[0].split.row_content_sha256[0]
    conflicting_split = inputs[1].split.model_copy(update={"row_content_sha256": hashes})
    identity = split_digest(conflicting_split)
    command.resolved.datasets[1].split_sha256 = identity
    changed = replace(
        inputs[1],
        split=conflicting_split,
        binding=inputs[1].binding.model_copy(update={"split_sha256": identity}),
    )
    with pytest.raises(TrainingValidationError, match="duplicate split"):
        compile_samples(command, [inputs[0], changed], SyntheticTokenizer())


def test_mixture_safetensors_optimizer_and_cursor_continuation(tmp_path: Path) -> None:
    command, inputs = frozen_sources(tmp_path)
    train, validation = compile_samples(command, inputs, SyntheticTokenizer())
    assert isinstance(train, MixtureSampler)
    weights = np.array([0.1, -0.2], dtype=np.float32)
    moments = np.zeros(2, dtype=np.float32)

    def update(sampler: Any, weight: Any, moment: Any, step: int) -> tuple[Any, Any]:
        batch = sampler.next_batch()
        gradient = np.array([row[1] / 1000 for row in batch.tokens], dtype=np.float32)
        moment = np.float32(0.9) * moment + np.float32(0.1) * gradient
        weight = weight - np.float32(0.001 / (step + 1)) * moment
        return weight, moment

    for step in range(2):
        weights, moments = update(train, weights, moments, step)
    assert (train.epoch, train.cursor) == (0, 4)
    state = CheckpointWorkerState(
        job_id=command.job_id,
        attempt_id=command.attempt_id,
        fence=command.fence,
        completed_update=2,
        rank=0,
        world_size=1,
        runtime_sha256=command.resolved.runtime_sha256,
        resolved_spec_sha256=resolved_digest(command),
        optimizer=command.resolved.spec.optim,
        mlx_rng_key=(123, 456),
        sampler=train.snapshot(),
    )
    store = CheckpointStore(tmp_path / "checkpoints", tensor_io=CpuTensorIO(), disk_floor_bytes=0)
    manifest = store.save(
        state, {"lora_a": weights}, {"step": np.array(2, dtype=np.uint32), "m": moments}
    )
    expected_order = []
    for step in range(2, 34):
        weights, moments = update(train, weights, moments, step)
        expected_order.append(train.last_batch_references)
    restored = store.restore(
        manifest.artifact_id,
        expected_runtime_sha256=command.resolved.runtime_sha256,
        expected_resolved_spec_sha256=resolved_digest(command),
    )
    resumed, _ = compile_samples(command, inputs, SyntheticTokenizer())
    assert isinstance(resumed, MixtureSampler)
    resumed.restore(restored.state.sampler)
    actual_weights, actual_moments = (
        restored.adapter_tensors["lora_a"],
        restored.optimizer_state["m"],
    )
    validation_state = validation.snapshot()
    for step in range(2, 34):
        actual_weights, actual_moments = update(resumed, actual_weights, actual_moments, step)
        assert resumed.last_batch_references == expected_order[step - 2]
    assert resumed.snapshot() == train.snapshot()
    assert validation.snapshot() == validation_state
    np.testing.assert_array_equal(actual_weights, weights)
    np.testing.assert_array_equal(actual_moments, moments)
    assert restored.optimizer_state["step"].item() == 2


def test_rank_components_publish_only_common_bundle_and_restore_selected_rank(
    tmp_path: Path,
) -> None:
    command, inputs = frozen_sources(tmp_path)
    command.resolved.spec.placement.mode = "data_parallel"
    command.resolved.spec.optim.batch_size = 2
    command.world_size = 2
    store = CheckpointStore(tmp_path / "ranks", tensor_io=CpuTensorIO(), disk_floor_bytes=0)
    artifact_id = uuid.uuid4()
    components = []
    for rank in range(2):
        prepared = command.model_copy(
            update={"rank": rank, "node": "coire-edge-a" if rank == 0 else "coire-edge-b"}
        )
        sampler, _ = compile_samples(prepared, inputs, SyntheticTokenizer())
        sampler.next_batch()
        state = CheckpointWorkerState(
            job_id=JOB,
            attempt_id=JOB,
            fence=1,
            completed_update=1,
            rank=rank,
            world_size=2,
            runtime_sha256="b" * 64,
            resolved_spec_sha256=resolved_digest(command),
            optimizer=command.resolved.spec.optim,
            mlx_rng_key=(123, rank),
            sampler=sampler.snapshot(),
        )
        components.append(
            store.save_rank(
                state,
                {"lora_a": np.array([rank], dtype=np.float32)},
                {"step": np.array(1, dtype=np.uint32), "m": np.array([rank + 1], dtype=np.float32)},
                artifact_id=artifact_id,
            )
        )
        assert not store.path_for(artifact_id).exists()
    with pytest.raises(TrainingConflict, match="incomplete"):
        store.publish_rank_bundle(components[:1], artifact_id=artifact_id)
    with pytest.raises(TrainingConflict, match="duplicated"):
        store.publish_rank_bundle([components[0], components[0]], artifact_id=artifact_id)
    altered = replace(components[1], state=components[1].state.model_copy(update={"fence": 2}))
    with pytest.raises(TrainingConflict, match="lineage"):
        store.publish_rank_bundle([components[0], altered], artifact_id=artifact_id)
    manifest = store.publish_rank_bundle(components, artifact_id=artifact_id)
    assert manifest.world_size == 2 and {rank.rank for rank in manifest.ranks} == {0, 1}
    assert {file.name for file in manifest.files} == {
        f"rank-{rank}-{name}"
        for rank in range(2)
        for name in ("adapter.safetensors", "optimizer.safetensors", "state.json")
    }
    with pytest.raises(TrainingConflict, match="world size"):
        store.restore(
            artifact_id,
            expected_runtime_sha256="b" * 64,
            expected_resolved_spec_sha256=resolved_digest(command),
        )
    for rank in range(2):
        restored = store.restore(
            artifact_id,
            expected_runtime_sha256="b" * 64,
            expected_resolved_spec_sha256=resolved_digest(command),
            rank=rank,
            expected_world_size=2,
        )
        assert restored.state.rank == rank
        assert restored.adapter_tensors["lora_a"].item() == rank
        assert restored.optimizer_state["m"].item() == rank + 1
    (store.path_for(artifact_id) / "rank-1-state.json").write_bytes(b"corrupt")
    # Corrupt peer components invalidate the bundle even when restoring rank zero.
    with pytest.raises(TrainingValidationError, match="bytes"):
        store.restore(
            artifact_id,
            expected_runtime_sha256="b" * 64,
            expected_resolved_spec_sha256=resolved_digest(command),
            rank=0,
            expected_world_size=2,
        )
