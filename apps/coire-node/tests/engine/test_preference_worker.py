"""Native objective callbacks persist pair metrics and complete fenced checkpoints."""

import shutil
import socket
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from coire_core.models.preference import TokenizedPreferenceExample
from coire_core.models.training import ResolvedTrainingSpecV3
from coire_core.models.training_node import (
    CheckpointCommitAcknowledgementV3,
    CheckpointWorkerStateV3,
    NodeTrainingEvent,
    TrainingArtifactManifest,
    TrainingPrepareRequest,
)

pytestmark = pytest.mark.engine


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
@pytest.mark.parametrize("pause", [False, True])
def test_preference_worker_metrics_validation_checkpoint_and_pause(
    training_model: Path,
    training_kind: Literal["lora", "qlora", "dora"],
    tmp_path: Path,
    objective: Literal["dpo", "orpo"],
    pause: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_training_worker import offline_command

    from coire_node.training.checkpoints import CheckpointStore
    from coire_node.training.preference_data import IndexedPreferenceSource, PreferenceSampler
    from coire_node.training.preference_runtime import (
        load_preference_runtime,
        validate_preference_input,
    )
    from coire_node.training.worker import (
        PausedAtCheckpoint,
        make_optimizer,
        resolved_digest,
        run_sft,
    )

    data = offline_command(training_model, training_kind).model_dump(mode="json")
    resolved = data["resolved"]
    spec = resolved["spec"]
    spec.update(
        schema_version=3,
        objective=objective,
        init_adapter=None,
        objective_options={"beta": 0.1} if objective == "dpo" else {"weight": 0.1},
    )
    spec["parameterization"]["dropout"] = 0
    source = validate_preference_input(
        training_model,
        offline_command(training_model, training_kind).resolved.spec.parameterization.model_copy(
            update={"dropout": 0}
        ),
    )
    resolved["base_manifest_sha256"] = source.manifest.sha256()
    target = {**spec["model"], "base_manifest_sha256": resolved["base_manifest_sha256"]}
    resolved.update(
        initial_target=target,
        reference_target=target if objective == "dpo" else None,
        sampler_version="coire-pair-sampler-v1",
    )
    resolved["resource_envelope"].update(
        reference_weight_bytes=1 if objective == "dpo" else 0, reference_adapter_bytes=0
    )
    command = TrainingPrepareRequest.model_validate(data)
    assert isinstance(command.resolved, ResolvedTrainingSpecV3)
    runtime = load_preference_runtime(
        source, seed=42, objective=objective, initial_target=command.resolved.initial_target
    )
    optimizer = make_optimizer(command.resolved.spec.optim)
    examples = [
        TokenizedPreferenceExample(
            source_row=i + 1,
            content_sha256=f"{i:064x}",
            prompt_sha256=f"{i:064x}",
            chosen_tokens=[1, 2 + i, 8, 9],
            rejected_tokens=[1, 2 + i, 7],
            chosen_mask=[False, False, True, True],
            rejected_mask=[False, False, True],
            prompt_length=2,
        )
        for i in range(4)
    ]

    def sampler(digest: str) -> PreferenceSampler:
        return PreferenceSampler(
            [IndexedPreferenceSource(uuid.uuid4(), digest * 64, (1, 2, 3, 4), 4, examples)],
            mixture_sha256=digest * 64,
            batch_size=1,
            seed=7,
            max_sequence_length=8,
        )

    training, validation = sampler("a"), sampler("b")
    validation_state = validation.snapshot()
    store = CheckpointStore(tmp_path / "checkpoints", disk_floor_bytes=0)
    events: list[NodeTrainingEvent] = []
    commits: list[int | None] = []

    def commit(manifest: TrainingArtifactManifest) -> CheckpointCommitAcknowledgementV3:
        restored = store.restore(
            manifest.artifact_id,
            expected_runtime_sha256=command.resolved.runtime_sha256,
            expected_resolved_spec_sha256=resolved_digest(command),
        )
        assert isinstance(restored.state, CheckpointWorkerStateV3)
        assert restored.state.objective == objective
        assert restored.state.completed_update == int(optimizer.step.item())
        commits.append(manifest.update)
        return CheckpointCommitAcknowledgementV3.model_validate(
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
                "schema_version": 3,
                "command_id": str(uuid.uuid4()),
                "checkpoint_id": str(manifest.artifact_id),
                "manifest_sha256": manifest.canonical_sha256(),
                "update": manifest.update,
                "committed_update": manifest.update,
                "job_version": 1,
                "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=29)).isoformat(),
            }
        )

    def execute() -> int:
        return run_sft(
            command,
            runtime.policy,
            optimizer,
            training,
            validation,
            preference=runtime,
            store=store,
            scratch=tmp_path / "scratch",
            emit=events.append,
            commit=commit,
            footprint=lambda: 0,
            control=lambda: "pause" if pause and optimizer.step.item() >= 2 else "continue",
        )

    if pause:
        with pytest.raises(PausedAtCheckpoint):
            execute()
    else:
        assert execute() == 4
    assert commits == ([2] if pause else [2, 4])
    assert validation.snapshot() == validation_state
    metrics = [
        event.payload.metric for event in events if event.payload.kind == "preference_progress"
    ]
    train = [m for m in metrics if m.kind == "train"]
    assert [m.update for m in train] == list(range(1, (2 if pause else 4) + 1))
    assert all(m.pair_count == 2 and m.response_tokens == 6 and m.probe is not None for m in train)
    assert all(m.objective == objective and m.fence == 1 for m in metrics)
    assert any(m.kind == "validation" for m in metrics)
    if not pause:
        completed = [
            event.payload.manifest for event in events if event.payload.kind == "checkpoint_staged"
        ]
        assert completed[-1].update == 4
        extract_and_serve(command, completed[-1], store.root, training_model, tmp_path, monkeypatch)


def extract_and_serve(
    command: TrainingPrepareRequest,
    checkpoint: TrainingArtifactManifest,
    checkpoint_root: Path,
    model: Path,
    temporary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real standalone extraction and authenticated node proxy, no engine-port client."""
    from coire_core.models.engine import EngineState
    from coire_core.models.training_node import TrainingAdapterExtractRequest
    from coire_node.testing.harness import Agent
    from coire_node.training.artifacts import TrainingArtifacts
    from coire_node.training.extraction import AdapterExtractor

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    agent = Agent(
        temporary / "serving",
        node_store_dir=str(model.parent),
        node_engine_start_timeout_s=120,
        node_engine_port_range=f"{port}-{port}",
    )
    engine_id = uuid.uuid4()
    try:
        artifacts = TrainingArtifacts(
            Path(agent.settings.node_state_dir) / "training" / "artifacts",
            node_name="coire-edge-a",
        )
        shutil.copytree(checkpoint_root, artifacts.root, dirs_exist_ok=True)
        adapter_id = uuid.uuid4()
        extraction = AdapterExtractor(
            artifacts, agent.reservations, agent.settings, agent.store
        ).extract(
            TrainingAdapterExtractRequest(
                command_id=uuid.uuid4(),
                adapter_id=adapter_id,
                checkpoint_id=checkpoint.artifact_id,
                checkpoint_manifest_sha256=checkpoint.canonical_sha256(),
                job_id=command.job_id,
                attempt_id=command.attempt_id,
                fence=command.fence,
                node="coire-edge-a",
                resolved=command.resolved,
                disk_reservation_id=uuid.uuid4(),
                max_bytes=64 * 1024**2,
                deadline=datetime.now(UTC) + timedelta(minutes=2),
            )
        )
        assert extraction.state == "succeeded" and extraction.manifest is not None
        assert isinstance(command.resolved, ResolvedTrainingSpecV3)
        target = command.resolved.initial_target.model_copy(
            update={
                "adapter_id": adapter_id,
                "adapter_manifest_sha256": extraction.manifest.canonical_sha256(),
            }
        )
        _, status = agent.engines.start(
            engine_id=engine_id,
            slug=model.name,
            estimate_bytes=2 * 1024**3,
            target=target,
        )
        deadline = time.monotonic() + 120
        while status.state is not EngineState.READY:
            assert status.state is not EngineState.FAILED and time.monotonic() < deadline
            time.sleep(0.1)
            current = agent.engines.get(engine_id)
            assert current is not None
            status = current
        assert status.target == target
        monkeypatch.setattr("coire_node.routes.engines._proxy_client", None)
        with agent.client() as gateway:
            response = gateway.post(
                f"/node/engines/{engine_id}/proxy/v1/chat/completions",
                json={
                    "model": model.name,
                    "messages": [{"role": "user", "content": "Say hello."}],
                    "max_tokens": 8,
                    "temperature": 0,
                    "top_k": 0,
                    "min_p": 0,
                    "seed": 77,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["choices"]
    finally:
        try:
            stopped = agent.engines.stop(engine_id)
            deadline = time.monotonic() + 5
            while (
                stopped is not None
                and stopped.state is EngineState.STOPPING
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
                stopped = agent.engines.get(engine_id)
            assert stopped is not None and stopped.state is EngineState.STOPPED
        finally:
            agent.close()
