"""CPU two-rank full-state transport simulation; never imports MLX or calls Studios."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from safetensors.numpy import load_file, save_file

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.datasets import TokenizedTrainingExample
from coire_core.models.training import TrainingOptimizer
from coire_core.models.training_node import (
    CheckpointWorkerState,
    TrainingArtifactManifest,
    TrainingArtifactVerificationReceipt,
    TrainingPrepareRequest,
)
from coire_node.training.checkpoints import CheckpointStore, RankCheckpointComponent
from coire_node.training.distributed import (
    BareCollective,
    DeadlineGuardian,
    JacclLaunch,
    RankCheckpointCoordinator,
    parameter_digest,
)
from coire_node.training.sampler import SingleSourceSampler

JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


class CpuTensors:
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


def rank_state(rank: int, update: int = 3) -> CheckpointWorkerState:
    sampler = SingleSourceSampler(
        [
            TokenizedTrainingExample(
                source_row=i + 1,
                content_sha256="a" * 64,
                tokens=[1, 2, 3],
                target_mask=[False, False, True],
                target_start=2,
            )
            for i in range(4)
        ],
        dataset_sha256="b" * 64,
        batch_size=2,
        seed=0,
        max_sequence_length=8,
    )
    sampler.next_batch()
    return CheckpointWorkerState(
        job_id=JOB,
        attempt_id=JOB,
        fence=1,
        completed_update=update,
        rank=rank,
        world_size=2,
        runtime_sha256="c" * 64,
        resolved_spec_sha256="d" * 64,
        optimizer=TrainingOptimizer(updates=32),
        mlx_rng_key=(123, 456 + rank),
        sampler=sampler.snapshot(),
    )


def owned(rank: int) -> TrainingPrepareRequest:
    # Coordinator uses the existing strict scope. Full resolved-intent behavior is
    # independently tested by node compilation contracts; this fixture supplies scope only.
    return TrainingPrepareRequest.model_construct(
        job_id=JOB,
        attempt_id=JOB,
        fence=1,
        rank=rank,
        world_size=2,
        node=("coire-edge-a", "coire-edge-b")[rank],
    )


class Pair:
    """Two independent CPU stores, bounded component rendezvous and verified copies."""

    def __init__(self, root: Path) -> None:
        self.stores = [
            CheckpointStore(root / f"node-{rank}", tensor_io=CpuTensors(), disk_floor_bytes=0)
            for rank in range(2)
        ]
        self.condition = threading.Condition()
        self.parts: dict[uuid.UUID, dict[int, RankCheckpointComponent]] = {}
        self.digests: list[str] = []
        self.cancelled = False
        self.mirror_failure = False
        self.bad_receipts = False

    def compare(self, digest: str) -> None:
        with self.condition:
            self.digests.append(digest)
            self.condition.notify_all()
            assert self.condition.wait_for(lambda: len(self.digests) == 2, timeout=2)
            if len(set(self.digests)) != 1:
                raise TrainingConflict("different manifest")

    def action(self, local: str) -> str:
        raise AssertionError("This transport simulation does not emulate MLX reductions")

    def publish(
        self, artifact_id: uuid.UUID, component: RankCheckpointComponent, deadline: float
    ) -> None:
        assert time.monotonic() < deadline
        with self.condition:
            self.parts.setdefault(artifact_id, {})[component.state.rank] = component
            self.condition.notify_all()

    def collect(
        self, artifact_id: uuid.UUID, state: CheckpointWorkerState, deadline: float
    ) -> list[RankCheckpointComponent]:
        with self.condition:
            end = min(deadline, time.monotonic() + 0.5)
            while len(self.parts[artifact_id]) != 2 and not self.cancelled:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TrainingConflict("peer rank absent")
                self.condition.wait(remaining)
            if self.cancelled:
                raise TrainingConflict("cancelled transport")
            components = list(self.parts[artifact_id].values())
        result = []
        for component in components:
            # Actual local copies, not shared pointers pretending to be replication.
            directory = (
                self.stores[state.rank].root / f"received-{artifact_id}-{component.state.rank}"
            )
            shutil.copytree(component.directory, directory)
            result.append(replace(component, directory=directory))
        return result

    def mirror(
        self, manifest: TrainingArtifactManifest, deadline: float
    ) -> list[TrainingArtifactVerificationReceipt]:
        if self.mirror_failure:
            raise TrainingConflict("data link unavailable")
        receipts = []
        for rank, store in enumerate(self.stores):
            assert time.monotonic() < deadline
            restored = store.restore(
                manifest.artifact_id,
                expected_runtime_sha256="c" * 64,
                expected_resolved_spec_sha256="d" * 64,
                rank=rank,
                expected_world_size=2,
            )
            assert restored.manifest.canonical_sha256() == manifest.canonical_sha256()
            receipts.append(
                TrainingArtifactVerificationReceipt(
                    command_id=uuid.uuid4(),
                    artifact_id=manifest.artifact_id,
                    manifest_sha256=manifest.canonical_sha256(),
                    node=("coire-edge-a", "coire-edge-b")[rank],
                    verified_bytes=manifest.total_bytes,
                )
            )
        return receipts[:1] if self.bad_receipts else receipts

    def run(
        self, rank: int, state: CheckpointWorkerState | None = None
    ) -> TrainingArtifactManifest:
        return RankCheckpointCoordinator(
            owned(rank),
            self.stores[rank],
            self,
            self,
            control=lambda: "cancel" if self.cancelled else "continue",
        ).checkpoint(
            state or rank_state(rank),
            {"lora_a": np.ones((2, 2), dtype=np.float32)},
            {"step": np.array(3, dtype=np.uint32), "moment": np.array([0.75], dtype=np.float32)},
        )


def test_two_rank_full_common_bundle_copies_preserve_each_rng_and_optimizer(tmp_path: Path) -> None:
    pair = Pair(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(pair.run, (0, 1)))
    assert results[0].canonical_sha256() == results[1].canonical_sha256()
    assert len(results[0].files) == 6
    for store in pair.stores:
        for rank in range(2):
            restored = store.restore(
                results[0].artifact_id,
                expected_runtime_sha256="c" * 64,
                expected_resolved_spec_sha256="d" * 64,
                rank=rank,
                expected_world_size=2,
            )
            assert restored.state.mlx_rng_key == (123, 456 + rank)
            assert restored.optimizer_state["step"].item() == 3
            assert restored.optimizer_state["moment"].item() == 0.75
            assert restored.state.sampler == rank_state(rank).sampler


def test_partial_start_never_advertises_complete_checkpoint(tmp_path: Path) -> None:
    pair = Pair(tmp_path)
    with pytest.raises(TrainingConflict, match="absent"):
        pair.run(0)
    assert not list(pair.stores[0].root.glob("*/manifest.json"))


def test_partial_write_does_not_replace_previous_common_checkpoint(tmp_path: Path) -> None:
    pair = Pair(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(pair.run, (0, 1)))
    previous = results[0]
    with pytest.raises(TrainingValidationError, match="counter differs"):
        pair.run(0, rank_state(0, update=4))
    for store in pair.stores:
        restored = store.newest_valid(
            [previous.artifact_id],
            expected_runtime_sha256="c" * 64,
            expected_resolved_spec_sha256="d" * 64,
            rank=1,
            expected_world_size=2,
        )
        assert restored.state.completed_update == 3


def test_link_loss_after_staging_leaves_controller_mirror_uncommitted(tmp_path: Path) -> None:
    pair = Pair(tmp_path)
    pair.mirror_failure = True
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(pair.run, (0, 1)))
    with pytest.raises(TrainingConflict, match="link unavailable"):
        pair.mirror(results[0], time.monotonic() + 1)


def test_bundle_staging_does_not_wait_for_controller_mirror_receipts(tmp_path: Path) -> None:
    pair = Pair(tmp_path)
    pair.bad_receipts = True
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(pair.run, (0, 1)))
    # Coordinator returns staging so the controller can see the complete bundle.
    # It does not fabricate a commit or consume the incomplete mirror evidence.
    assert len(pair.mirror(results[0], time.monotonic() + 1)) == 1


def test_cancellation_interrupts_component_wait_without_manifest(tmp_path: Path) -> None:
    pair = Pair(tmp_path)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(pair.run, 0)
        with pair.condition:
            assert pair.condition.wait_for(lambda: bool(pair.parts), timeout=2)
            pair.cancelled = True
            pair.condition.notify_all()
        with pytest.raises(TrainingConflict, match="cancelled"):
            future.result(timeout=2)
    assert not list(pair.stores[0].root.glob("*/manifest.json"))


def test_rank_cursor_disagreement_and_changed_fence_reject_bundle(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path, tensor_io=CpuTensors(), disk_floor_bytes=0)
    identity = uuid.uuid4()
    states = [rank_state(rank) for rank in (0, 1)]
    states[1] = states[1].model_copy(update={"fence": 2})
    components = [
        store.save_rank(
            state,
            {"lora_a": np.ones(1, dtype=np.float32)},
            {"step": np.array(3, dtype=np.uint32)},
            artifact_id=identity,
        )
        for state in states
    ]
    with pytest.raises(TrainingConflict, match="lineage"):
        store.publish_rank_bundle(components, artifact_id=identity)
    assert not store.path_for(identity).exists()


def test_guardian_renewal_does_not_resurrect_stalled_collective() -> None:
    now = [10.0]
    guard = DeadlineGuardian(seconds=5, clock=lambda: now[0])
    now[0] = 14.0
    guard.progress()
    now[0] = 19.0
    assert guard.expired()
    with pytest.raises(TrainingConflict, match="expired"):
        guard.progress()


def test_parameter_hash_compares_raw_values_names_shape_and_dtype() -> None:
    codec = CpuTensors()
    value = np.array([1, 2], dtype=np.float32)
    digest = parameter_digest({"a": value}, codec)
    assert digest == parameter_digest({"a": value.copy()}, codec)
    for changed in (
        {"a": value + 1},
        {"b": value},
        {"a": value.reshape(1, 2)},
        {"a": value.astype(np.float16)},
    ):
        assert digest != parameter_digest(changed, codec)


def test_native_generated_hostfile_environment_no_ssh_or_caller_hosts(tmp_path: Path) -> None:
    path = tmp_path / "hostfile.json"
    value: dict[str, Any] = {
        "backend": "jaccl",
        "hosts": [
            {"ssh": "coire-edge-a.fabric", "rdma": [None, "rdma_en2"]},
            {"ssh": "coire-edge-b.fabric", "rdma": ["rdma_en3", None]},
        ],
    }
    path.write_text(json.dumps(value))
    launch = JacclLaunch(path, hashlib.sha256(path.read_bytes()).hexdigest(), 32323)
    directory = tmp_path / "rank"
    directory.mkdir(mode=0o700)
    env = launch.environment(owned(1), directory)
    assert env["MLX_RANK"] == "1"
    assert env["MLX_JACCL_COORDINATOR"] == "coire-edge-a.fabric:32323"
    assert json.loads(Path(env["MLX_IBV_DEVICES"]).read_bytes()) == [
        [None, "rdma_en2"],
        ["rdma_en3", None],
    ]
    value["hosts"][0]["ssh"] = "caller.example"
    path.write_text(json.dumps(value))
    with pytest.raises(TrainingConflict, match="identity"):
        launch.devices()
    with pytest.raises(TrainingValidationError, match="declared pair"):
        replace(launch, hostfile_sha256=hashlib.sha256(path.read_bytes()).hexdigest()).devices()


def test_bare_collective_attaches_strict_group_and_detects_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_node.training.checkpoints import MlxTensorIO

    calls: list[dict[str, Any]] = []
    peer_differs = [False]
    group = SimpleNamespace(size=lambda: 2, rank=lambda: 1)

    def init(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return group

    def gather(value: Any, **kwargs: Any) -> Any:
        assert kwargs == {"group": group, "stream": "cpu"}
        peer = value.copy()
        if peer_differs[0]:
            peer[0] ^= 1
        return np.concatenate((peer, value))

    fake = SimpleNamespace(
        array=lambda value, **kwargs: np.array(value, dtype=np.uint8),
        uint8="uint8",
        cpu="cpu",
        eval=lambda *values: None,
        distributed=SimpleNamespace(
            init=init,
            all_gather=gather,
            all_max=lambda value, **kwargs: np.array(max(value.item(), 1)),
        ),
    )
    monkeypatch.setattr(MlxTensorIO, "module", staticmethod(lambda: fake))
    collective = BareCollective(owned(1))
    assert calls == [{"strict": True, "backend": "jaccl"}]
    collective.compare("a" * 64)
    peer_differs[0] = True
    with pytest.raises(TrainingConflict, match="disagree"):
        collective.compare("a" * 64)
    assert collective.action("continue") == "pause"
    assert collective.action("kill") == "kill"
    with pytest.raises(TrainingConflict, match="authority"):
        collective.action("invalid")
    with pytest.raises(TrainingConflict, match="rank identity"):
        BareCollective(owned(0))
