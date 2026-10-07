"""Opt-in offline bare worker evaluation/checkpoint/pause gate; never runs on core."""

import hashlib
import json
import platform
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

from coire_core.models.datasets import TokenizedTrainingExample
from coire_core.models.training_node import (
    CheckpointCommitAcknowledgement,
    CheckpointWorkerState,
    TrainingArtifactManifest,
    TrainingPrepareRequest,
)
from coire_node.training.checkpoints import CheckpointStore
from coire_node.training.objectives import load_sft_runtime, validate_sft_input
from coire_node.training.sampler import SingleSourceSampler
from coire_node.training.worker import PausedAtCheckpoint, make_optimizer, resolved_digest, run_sft

pytestmark = pytest.mark.engine


def test_measurement_boundary_serializes_real_state_without_durable_commit(
    training_model: Path, tmp_path: Path, training_kind: Literal["lora", "qlora", "dora"]
) -> None:
    command = offline_command(training_model, training_kind)
    source = validate_sft_input(training_model, command.resolved.spec.parameterization)
    command.resolved.base_manifest_sha256 = hashlib.sha256(
        source.manifest.canonical_bytes()
    ).hexdigest()
    runtime = load_sft_runtime(source, seed=42)
    optimizer = make_optimizer(command.resolved.spec.optim)
    examples = [
        TokenizedTrainingExample(
            source_row=i + 1,
            content_sha256="c" * 64,
            tokens=[1, 2 + i, 8, 9],
            target_mask=[False, True, True, True],
            target_start=1,
        )
        for i in range(4)
    ]

    def sampler(digest: str) -> SingleSourceSampler:
        return SingleSourceSampler(
            examples, dataset_sha256=digest * 64, batch_size=1, seed=7, max_sequence_length=8
        )

    store = CheckpointStore(tmp_path / "private-measurement", disk_floor_bytes=0)
    events: list[Any] = []
    boundaries: list[int] = []

    def measure(state: CheckpointWorkerState, adapter: dict[str, Any], moments: Any) -> None:
        assert state.completed_update == int(optimizer.step.item())
        manifest = store.save(state, adapter, moments)
        restored = store.restore(
            manifest.artifact_id,
            expected_runtime_sha256=command.resolved.runtime_sha256,
            expected_resolved_spec_sha256=resolved_digest(command),
        )
        assert restored.state.completed_update == state.completed_update
        assert restored.adapter_tensors and restored.optimizer_state
        boundaries.append(state.completed_update)

    def no_commit(manifest: TrainingArtifactManifest) -> CheckpointCommitAcknowledgement | None:
        raise AssertionError("measurement requested a durable commit")

    completed = run_sft(
        command,
        runtime,
        optimizer,
        sampler("a"),
        sampler("b"),
        store=store,
        scratch=tmp_path / "scratch",
        emit=events.append,
        commit=no_commit,
        control=lambda: "continue",
        footprint=lambda: 1,
        measurement_checkpoint=measure,
    )
    assert completed == 4 and boundaries == [2, 4]
    assert all(
        event.payload.kind not in {"checkpoint_staged", "checkpoint_rank_staged"}
        for event in events
    )
    assert {event.payload.metric.kind for event in events if event.payload.kind == "progress"} == {
        "train",
        "validation",
    }


def offline_command(
    training_model: Path, training_kind: Literal["lora", "qlora", "dora"]
) -> TrainingPrepareRequest:
    ids = [str(uuid.uuid4()) for _ in range(3)]
    command = TrainingPrepareRequest.model_validate(
        {
            "command_id": str(uuid.uuid4()),
            "job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "attempt_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "fence": 1,
            "request_sha256": "a" * 64,
            "node": platform.node().split(".", 1)[0],
            "rank": 0,
            "world_size": 1,
            "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=29)).isoformat(),
            "reservation_id": str(uuid.uuid4()),
            "disk_reservation_id": str(uuid.uuid4()),
            "resolved": {
                "spec": {
                    "model": {"model_id": ids[0], "variant_id": ids[1]},
                    "data": {
                        "train": {
                            "datasets": [
                                {"dataset_id": ids[2], "sample_count": 4, "mixture_proportion": 1.0}
                            ],
                            "epoch_samples": 4,
                        },
                        "validation": {"dataset_ids": [ids[2]]},
                    },
                    "parameterization": {
                        "kind": training_kind,
                        "rank": 2,
                        "dropout": 0.15,
                        "target_modules": ["self_attn.q_proj", "self_attn.v_proj"],
                    },
                    "optim": {
                        "updates": 4,
                        "accumulation_steps": 2,
                        "max_sequence_length": 8,
                        "learning_rate": 0.001,
                        "schedule": {"kind": "warmup_linear", "warmup_updates": 2},
                    },
                    "eval": {"loss_every_updates": 2},
                    "output": {"adapter_slug": "offline-worker", "checkpoint_every_updates": 2},
                },
                "base_manifest_sha256": "b" * 64,
                "datasets": [
                    {
                        "dataset_id": ids[2],
                        "analysis_id": str(uuid.uuid4()),
                        "source_sha256": "c" * 64,
                        "split_sha256": "d" * 64,
                        "analysis_sha256": "e" * 64,
                    }
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
                    "buffer_bytes": 0,
                    "safety_bytes": 1,
                    "checkpoint_bytes": 1,
                    "evidence_sha256": "c" * 64,
                },
            },
        }
    )
    return command


@pytest.mark.parametrize("pause", [False, True])
def test_worker_bare_evaluation_full_checkpoint_and_pause(
    training_model: Path,
    tmp_path: Path,
    pause: bool,
    training_kind: Literal["lora", "qlora", "dora"],
) -> None:
    """No download. The fixture fails missing prerequisites when explicitly enabled."""
    command = offline_command(training_model, training_kind)
    source = validate_sft_input(training_model, command.resolved.spec.parameterization)
    command.resolved.base_manifest_sha256 = hashlib.sha256(
        source.manifest.canonical_bytes()
    ).hexdigest()
    runtime = load_sft_runtime(source, seed=42)
    optimizer = make_optimizer(command.resolved.spec.optim)
    examples = [
        TokenizedTrainingExample(
            source_row=i + 1,
            content_sha256="c" * 64,
            tokens=[1, 2 + i, 8, 9],
            target_mask=[False, True, True, True],
            target_start=1,
        )
        for i in range(4)
    ]

    def sampler(digest: str) -> SingleSourceSampler:
        return SingleSourceSampler(
            examples, dataset_sha256=digest * 64, batch_size=1, seed=7, max_sequence_length=8
        )

    training, held_out = sampler("a"), sampler("b")
    held_out_state = held_out.snapshot()
    store = CheckpointStore(tmp_path / "checkpoints", disk_floor_bytes=0)
    events: list[Any] = []
    committed: list[Any] = []

    def commit(manifest: Any) -> CheckpointCommitAcknowledgement:
        # Offline hook gate only: validate genuine full-state bytes locally. This
        # deliberately does not assert production two-copy controller durability.
        restored = store.restore(
            manifest.artifact_id,
            expected_runtime_sha256=command.resolved.runtime_sha256,
            expected_resolved_spec_sha256=resolved_digest(command),
        )
        assert restored.state.completed_update == optimizer.step.item()
        committed.append(manifest)
        return CheckpointCommitAcknowledgement.model_validate(
            {
                **command.model_dump(
                    mode="json",
                    exclude={
                        "resolved",
                        "reservation_id",
                        "disk_reservation_id",
                        "resume_manifest_sha256",
                        "resume_checkpoint_id",
                        "collective",
                    },
                ),
                "command_id": str(uuid.uuid4()),
                "checkpoint_id": str(manifest.artifact_id),
                "manifest_sha256": manifest.canonical_sha256(),
                "update": manifest.update,
                "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=29)).isoformat(),
            }
        )

    def execute() -> int:
        return run_sft(
            command,
            runtime,
            optimizer,
            training,
            held_out,
            store=store,
            scratch=tmp_path / "scratch",
            emit=events.append,
            commit=commit,
            control=lambda: "pause" if pause and optimizer.step.item() >= 2 else "continue",
            footprint=lambda: 0,
        )

    if pause:
        with pytest.raises(PausedAtCheckpoint):
            execute()
        assert optimizer.step.item() == 2
    else:
        assert execute() == 4
    assert held_out.snapshot() == held_out_state
    assert [item.update for item in committed] == ([2] if pause else [2, 4])
    assert any(
        event.payload.kind == "progress" and event.payload.metric.kind == "validation"
        for event in events
    )


@pytest.mark.asyncio
async def test_offline_native_cli_full_frozen_binding_and_local_replica_commit(
    training_model: Path,
    tmp_path: Path,
    training_kind: Literal["lora", "qlora", "dora"],
) -> None:
    """Actual owned worker process; two isolated local stores, no Studio or network calls."""
    import asyncio
    import shutil
    import sys
    import threading
    import time

    from coire_core.models.datasets import DatasetAnalysisBinding, DatasetFormat, SplitManifest
    from coire_core.models.training_node import (
        DatasetAnalysisWorkerInput,
        TrainingLeaseRenewal,
        TrainingStartRequest,
        TrainingStopRequest,
    )
    from coire_core.training_data import normalize_row, split_digest
    from coire_node.training.datasets import analyze_source, load_analysis_tokenizer
    from coire_node.training.journal import TrainingJournal
    from coire_node.training.supervisor import NativeProcesses, TrainingSupervisor
    from coire_node.training.worker import FrozenInputs, payload_sha256

    command = offline_command(training_model, training_kind)
    command.resolved.spec.data.loss_policy = "all_tokens"
    envelope = command.resolved.resource_envelope
    envelope.weight_bytes, envelope.adapter_bytes, envelope.optimizer_bytes = (
        1024**3,
        64 * 1024**2,
        256 * 1024**2,
    )
    envelope.activation_bytes, envelope.buffer_bytes, envelope.safety_bytes = (
        1024**3,
        128 * 1024**2,
        2 * 1024**3,
    )
    envelope.checkpoint_bytes = 128 * 1024**2
    model = validate_sft_input(training_model, command.resolved.spec.parameterization)
    command.resolved.base_manifest_sha256 = hashlib.sha256(
        model.manifest.canonical_bytes()
    ).hexdigest()
    selected = command.resolved.datasets[0]
    rows = [{"text": f"Synthetic offline row {index}."} for index in range(6)]
    source = tmp_path / "source.jsonl"
    source.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
    source.chmod(0o600)
    selected.source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    split = SplitManifest(
        dataset_id=selected.dataset_id,
        source_sha256=selected.source_sha256,
        seed=0,
        train_rows=[1, 2, 3, 4],
        validation_rows=[5, 6],
        row_content_sha256=[
            normalize_row(
                row, format=DatasetFormat.TEXT, dataset_id=selected.dataset_id, source_row=index + 1
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
        model_slug=training_model.name,
        format=DatasetFormat.TEXT,
    )
    tokenizer, tokenizer_sha, template_sha, runtime_sha = load_analysis_tokenizer(
        training_model, binding
    )
    (
        command.resolved.tokenizer_sha256,
        command.resolved.template_sha256,
        command.resolved.runtime_sha256,
    ) = tokenizer_sha, template_sha, runtime_sha
    # Text fixture needs a tokenizer-specific bound, never silent truncation.
    command.resolved.spec.optim.max_sequence_length = 128
    analysis = analyze_source(
        source,
        DatasetAnalysisWorkerInput(
            command_id=uuid.uuid4(),
            analysis_id=selected.analysis_id,
            binding=binding,
            source_bytes=source.stat().st_size,
            memory_bytes=1024**3,
            deadline=datetime.now(UTC) + timedelta(seconds=29),
        ),
        tokenizer,
        tokenizer_sha256=tokenizer_sha,
        template_sha256=template_sha,
        runtime_sha256=runtime_sha,
        max_sequence_length=128,
    )
    assert analysis.state == "succeeded"
    selected.analysis_sha256 = payload_sha256(analysis)
    command.lease_expires_at = datetime.now(UTC) + timedelta(seconds=29)
    journal = TrainingJournal(
        tmp_path / "node", node=command.node, admission_lock=threading.RLock()
    )
    # The opt-in source gate can run from an isolated staging tree without
    # replacing the installed node build. Only the two packages under test are
    # added to this test child's module search path; production strips PYTHONPATH.
    import coire_core
    import coire_node

    class SourceProcesses(NativeProcesses):
        def spawn(self, argv: list[str], env: dict[str, str]) -> tuple[int, float]:
            assert "PYTHONPATH" not in env
            roots = []
            for package in (coire_core, coire_node):
                assert package.__file__ is not None
                roots.append(Path(package.__file__).parent.parent)
            return super().spawn(argv, {**env, "PYTHONPATH": ":".join(str(root) for root in roots)})

    native = SourceProcesses()
    supervisor = TrainingSupervisor(
        journal,
        interpreter=Path(sys.executable),
        store_root=training_model.parent,
        artifact_root=tmp_path / "training" / "artifacts",
        processes=native,
    )
    journal.prepare(
        command, memory_available=envelope.memory_bytes, disk_available=8 * 1024**3, disk_floor=0
    )
    await supervisor.bind_inputs(
        command, FrozenInputs(binding, split, analysis, source), disk_available=8 * 1024**3
    )
    wire = command.model_dump(
        mode="json",
        exclude={
            "resolved",
            "reservation_id",
            "disk_reservation_id",
            "resume_manifest_sha256",
            "resume_checkpoint_id",
            "collective",
        },
    )
    request = TrainingStartRequest.model_validate(
        {
            **wire,
            "command_id": str(uuid.uuid4()),
            "prepared_command_id": str(command.command_id),
            "spawn_nonce": str(uuid.uuid4()),
        }
    )
    started = False
    committed: set[uuid.UUID] = set()
    cursor = 0
    renew_at = time.monotonic() + 8
    replica = CheckpointStore(tmp_path / "replica", disk_floor_bytes=0)

    def mirror(manifest: Any) -> None:
        shutil.copytree(
            supervisor.artifact_root / str(manifest.artifact_id),
            replica.path_for(manifest.artifact_id),
        )
        restored = replica.restore(
            manifest.artifact_id,
            expected_runtime_sha256=runtime_sha,
            expected_resolved_spec_sha256=resolved_digest(command),
        )
        assert restored.manifest.canonical_sha256() == manifest.canonical_sha256()

    try:
        receipt = await supervisor.start(request)
        started = True
        async with asyncio.timeout(120):
            while True:
                if time.monotonic() >= renew_at:
                    await supervisor.renew(
                        TrainingLeaseRenewal.model_validate(
                            {
                                **wire,
                                "command_id": str(uuid.uuid4()),
                                "lease_expires_at": (
                                    datetime.now(UTC) + timedelta(seconds=29)
                                ).isoformat(),
                            }
                        )
                    )
                    renew_at = time.monotonic() + 8
                page = journal.events(command.attempt_id, cursor)
                cursor = page.next_sequence
                for event in page.items:
                    assert event.payload.kind != "failure"
                    if event.payload.kind == "checkpoint_staged":
                        manifest = event.payload.manifest
                        await asyncio.to_thread(mirror, manifest)
                        await supervisor.acknowledge_checkpoint(
                            CheckpointCommitAcknowledgement.model_validate(
                                {
                                    **wire,
                                    "command_id": str(uuid.uuid4()),
                                    "checkpoint_id": str(manifest.artifact_id),
                                    "manifest_sha256": manifest.canonical_sha256(),
                                    "update": manifest.update,
                                    "lease_expires_at": (
                                        datetime.now(UTC) + timedelta(seconds=29)
                                    ).isoformat(),
                                }
                            )
                        )
                        committed.add(manifest.artifact_id)
                status = await asyncio.to_thread(supervisor.observe, command.attempt_id)
                if status.liveness == "stopped":
                    assert status.update == 4 and len(committed) == 2
                    assert status.latest_manifest_sha256 is not None
                    assert native.children[receipt.pid].returncode == 0
                    break
                assert status.liveness == "running"
                await asyncio.sleep(0.1)
        await serve_completed_checkpoint(command, supervisor, tmp_path, training_model)
    finally:
        if started:
            stop_receipt = await supervisor.stop(
                TrainingStopRequest.model_validate(
                    {**wire, "command_id": str(uuid.uuid4()), "reason": "cancelled"}
                )
            )
            assert stop_receipt.stopped
        journal.close()


async def serve_completed_checkpoint(
    command: TrainingPrepareRequest,
    supervisor: Any,
    tmp_path: Path,
    training_model: Path,
) -> None:
    """Genuine extracted adapter served through authenticated node-owned bare MLX."""
    import asyncio

    import httpx

    from coire_core.models.adapters import InferenceTarget
    from coire_core.models.engine import EngineStartRequest, EngineState, EngineStatus
    from coire_core.models.gateway import ChatMessage, EngineChatRequest
    from coire_core.models.node import NetworkPath
    from coire_core.models.training_node import (
        TrainingAdapterExtractionStatus,
        TrainingAdapterExtractRequest,
    )
    from coire_node.routes.engines import close_engine_client
    from coire_node.testing.harness import TOKEN, Agent
    from coire_node.training.artifacts import TrainingArtifacts
    from coire_node.training.extraction import AdapterExtractor

    node = Agent(
        tmp_path / "serving",
        node_name=command.node,
        node_store_dir=str(training_model.parent),
        node_state_dir=str(tmp_path),
        training_enabled=True,
        legacy_network_mode=False,
    )
    engine_id = uuid.uuid4()
    artifacts = TrainingArtifacts(supervisor.artifact_root, node_name=command.node)
    store = CheckpointStore(supervisor.artifact_root, disk_floor_bytes=0)
    manifest = next(
        store.manifest(uuid.UUID(path.name))
        for path in supervisor.artifact_root.iterdir()
        if path.is_dir()
        and not path.name.startswith(".")
        and store.manifest(uuid.UUID(path.name)).update == 4
    )
    extraction = TrainingAdapterExtractRequest(
        command_id=uuid.uuid4(),
        adapter_id=uuid.uuid4(),
        checkpoint_id=manifest.artifact_id,
        checkpoint_manifest_sha256=manifest.canonical_sha256(),
        job_id=command.job_id,
        attempt_id=command.attempt_id,
        fence=command.fence,
        node=command.node,
        resolved=command.resolved,
        disk_reservation_id=uuid.uuid4(),
        max_bytes=128 * 1024**2,
        deadline=datetime.now(UTC) + timedelta(seconds=60),
    )
    app = node.app(NetworkPath.CONTROL)
    AdapterExtractor(artifacts, node.reservations, node.settings, node.store).attach(app)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://isolated-node",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=60,
        ) as client:
            response = await client.post(
                "/node/training/adapters/extractions",
                json=extraction.model_dump(mode="json"),
            )
            assert response.status_code == 200
            async with asyncio.timeout(60):
                while True:
                    observed = await client.get(
                        f"/node/training/adapters/extractions/{extraction.command_id}"
                    )
                    result = TrainingAdapterExtractionStatus.model_validate(observed.json())
                    assert result.state != "failed"
                    if result.state == "succeeded":
                        break
                    await asyncio.sleep(0.05)
            assert result.manifest is not None
            target = InferenceTarget(
                model_id=command.resolved.spec.model.model_id,
                variant_id=command.resolved.spec.model.variant_id,
                adapter_id=extraction.adapter_id,
                base_manifest_sha256=command.resolved.base_manifest_sha256,
                adapter_manifest_sha256=result.manifest.canonical_sha256(),
            )
            response = await client.post(
                "/node/engines",
                json=EngineStartRequest(
                    engine_id=engine_id,
                    slug=training_model.name,
                    estimate_bytes=2 * 1024**3,
                    target=target,
                ).model_dump(mode="json"),
            )
            assert response.status_code == 202
            node.engines.start_health_loop()
            async with asyncio.timeout(60):
                while True:
                    response = await client.get(f"/node/engines/{engine_id}")
                    status = EngineStatus.model_validate(response.json())
                    assert status.target == target and status.state is not EngineState.FAILED
                    if status.state is EngineState.READY:
                        break
                    await asyncio.sleep(0.1)
            response = await client.post(
                f"/node/engines/{engine_id}/proxy/v1/chat/completions",
                json=EngineChatRequest(
                    model=training_model.name,
                    messages=[ChatMessage(role="user", content="Reply with one short word.")],
                    max_tokens=8,
                    temperature=0,
                ).model_dump(mode="json"),
            )
            assert response.status_code == 200
            assert response.json()["choices"][0]["message"]["content"]
            served = node.engines.get(engine_id)
            assert served is not None and served.target == target
    finally:
        try:
            await close_engine_client()
            if node.engines.get(engine_id) is not None:
                node.engines.stop(engine_id)
                async with asyncio.timeout(20):
                    while True:
                        stopped = node.engines.get(engine_id)
                        assert stopped is not None
                        if stopped.state is EngineState.STOPPED:
                            break
                        await asyncio.sleep(0.05)
        finally:
            await asyncio.to_thread(node.close)
