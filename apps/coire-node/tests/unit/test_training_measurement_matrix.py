"""Real CPU source/checkpoint files verify matrix plumbing, not MLX/JACCL numerics."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import psutil
import pytest
from safetensors.numpy import load_file, save_file
from training_measurement_fixtures import mixture_experiment, observation

from coire_core.errors import TrainingConflict
from coire_core.models.training_node import (
    CheckpointWorkerState,
    TrainingMeasurementObservation,
    TrainingMeasurementRankCheckpoint,
    TrainingMeasurementRankSet,
)
from coire_core.settings import Settings
from coire_node.training.checkpoints import CheckpointStore, RankCheckpointComponent
from coire_node.training.distributed import DeadlineGuardian
from coire_node.training.journal import TrainingJournal
from coire_node.training.measurement import (
    RankMeasurementCheckpoint,
    probe_inputs,
    python_buffer_bytes,
)
from coire_node.training.sampler import MixtureSampler
from coire_node.training.supervisor import TrainingSupervisor
from coire_node.training.worker import compile_samples, resolved_digest
from coire_scheduler.training_measurements import build_report


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
        raise AssertionError("Synthetic raw text must not invoke a chat template")


class CpuRankExchange:
    """Two actual CPU serializers rendezvous; no native group or tensor estimate."""

    def __init__(self) -> None:
        self.barrier = threading.Barrier(2, timeout=3)
        self.ranks: dict[int, TrainingMeasurementRankCheckpoint] = {}
        self.lock = threading.Lock()

    def exchange(self, local: TrainingMeasurementRankCheckpoint) -> TrainingMeasurementRankSet:
        with self.lock:
            self.ranks[local.rank] = local
        self.barrier.wait()
        return TrainingMeasurementRankSet(ranks=list(self.ranks.values()))


class RecordingStore(CheckpointStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root, tensor_io=CpuTensorIO(), disk_floor_bytes=0)
        self.saved_state: CheckpointWorkerState | None = None
        self.saved_files: dict[str, bytes] = {}

    def save_rank(
        self,
        state: CheckpointWorkerState,
        adapter: dict[str, Any],
        optimizer_state: Any,
        *,
        artifact_id: Any,
    ) -> RankCheckpointComponent:
        component = super().save_rank(state, adapter, optimizer_state, artifact_id=artifact_id)
        self.saved_files = {
            f.name: (component.directory / f.name).read_bytes() for f in component.files
        }
        self.saved_state = CheckpointWorkerState.model_validate_json(
            (component.directory / f"rank-{state.rank}-state.json").read_bytes()
        )
        return component


@pytest.mark.parametrize("world_size", [1, 2])
async def test_probe_stages_and_compiles_every_source_and_separate_held_out(
    tmp_path: Path,
    world_size: int,
) -> None:
    _, dispatch, inputs = mixture_experiment(tmp_path, world_size=world_size)
    for probe in dispatch.commands:
        prepared = probe.prepare
        journal = TrainingJournal(
            tmp_path / f"journal-{prepared.rank}",
            node=prepared.node,
            admission_lock=threading.RLock(),
        )
        journal.prepare(prepared, memory_available=16 * 1024**3, disk_available=100 * 1024**3)
        supervisor = TrainingSupervisor(
            journal, interpreter=Path("/not-executed/python"), validate_ready=lambda _: None
        )
        for source in inputs:
            await supervisor.bind_inputs(prepared, source, disk_available=100 * 1024**3, multi=True)
        frozen = probe_inputs(prepared, supervisor.directory(prepared.attempt_id), journal)
        assert {s.binding.dataset_id for s in frozen} == {s.binding.dataset_id for s in inputs}
        train, held_out = compile_samples(prepared, frozen, SyntheticTokenizer())
        assert isinstance(train, MixtureSampler) and isinstance(held_out, MixtureSampler)
        assert len(train.sources) == 2 and len(held_out.sources) == 1
        assert (
            held_out.sources[0].dataset_id == prepared.resolved.spec.data.validation.dataset_ids[0]
        )
        assert train.world_size == world_size and train.rank == prepared.rank
        first = train.next_batch()
        assert len(first.tokens) == prepared.resolved.spec.optim.batch_size // world_size
        assert python_buffer_bytes(vars(train)) > sum(len(e.tokens) for e in train.dataset)
        # Exact global cursor survives the existing full state serializer on CPU.
        snapshot = train.snapshot()
        expected = [train.next_batch() for _ in range(4)]
        restored, _ = compile_samples(prepared, frozen, SyntheticTokenizer())
        restored.restore(snapshot)
        assert [restored.next_batch() for _ in range(4)] == expected
        journal.close()


def test_both_real_cpu_rank_states_determine_common_checkpoint_size(tmp_path: Path) -> None:
    row, dispatch, inputs = mixture_experiment(tmp_path, world_size=2)
    exchange = CpuRankExchange()
    checkpoints = []
    stores = []
    samplers = []
    for probe in dispatch.commands:
        prepared = probe.prepare
        train, _ = compile_samples(prepared, inputs, SyntheticTokenizer())
        train.next_batch()
        train.next_batch()
        samplers.append(train)
        store = RecordingStore(tmp_path / f"artifacts-{prepared.rank}")
        stores.append(store)
        checkpoints.append(
            RankMeasurementCheckpoint(
                probe,
                store,
                exchange,
                DeadlineGuardian(seconds=5),
                control=lambda: "continue",
                sample=lambda: int(psutil.Process().memory_info().rss),
                serializing=threading.Event(),
            )
        )

    def serialize(rank: int) -> None:
        prepared = dispatch.commands[rank].prepare
        state = CheckpointWorkerState(
            job_id=prepared.job_id,
            attempt_id=prepared.attempt_id,
            fence=prepared.fence,
            completed_update=2,
            rank=rank,
            world_size=2,
            runtime_sha256=prepared.resolved.runtime_sha256,
            resolved_spec_sha256=resolved_digest(prepared),
            optimizer=prepared.resolved.spec.optim,
            mlx_rng_key=(1, 2),
            sampler=samplers[rank].snapshot(),
        )
        checkpoints[rank].checkpoint(
            state,
            {"adapter.lora_a": np.arange(4, dtype=np.float32)},
            {"step": np.array(2, dtype=np.int32), "moment": np.zeros(16, dtype=np.float32)},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(serialize, [0, 1]))
    pair = checkpoints[0].largest
    assert pair is not None and checkpoints[1].largest == pair
    assert pair.serialized_bytes == sum(r.serialized_bytes for r in pair.ranks)
    assert pair.serialized_bytes > max(r.serialized_bytes for r in pair.ranks)
    for rank, store in enumerate(stores):
        state = store.saved_state
        assert (
            state is not None and state.rank == rank and state.sampler == samplers[rank].snapshot()
        )
        assert len(store.saved_files) == 3
        assert all(not p.is_file() for p in store.root.rglob("*"))
        assert (
            json.loads(store.saved_files[f"rank-{rank}-state.json"])["sampler"]["world_size"] == 2
        )
    measured = [
        TrainingMeasurementObservation.model_validate(
            {
                **observation(probe).model_dump(mode="json"),
                "rank": probe.prepare.rank,
                "world_size": 2,
                "rank_checkpoints": pair.model_dump(mode="json")["ranks"],
                "checkpoint_bytes": pair.serialized_bytes,
            }
        )
        for probe in dispatch.commands
    ]
    report = build_report(row, dispatch, measured, [], Settings())
    assert report.memory_evidence is not None
    assert report.memory_evidence.resource_envelope.checkpoint_bytes == pair.serialized_bytes
    assert report.memory_evidence.resource_envelope.weight_bytes == measured[0].weight_bytes
    with pytest.raises(TrainingConflict, match="every physical"):
        build_report(row, dispatch, measured[:1], [], Settings())
    assert report.memory_evidence.nodes[0].node != report.memory_evidence.nodes[1].node
