"""Real offline training/extraction and durable automatic evaluation orchestration.

The isolated node owns bare engines. Database admission/replication transport is
injected; this does not replace authenticated, two-Studio release acceptance.
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from evaluation_engine_fixtures import evaluation_engine, phase
from evaluation_training_fixtures import seed_evaluated_training
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import (
    AgentRunRow,
    Base,
    EvaluationAttemptRow,
    EvaluationGroupRow,
    EvaluationResultRow,
    EvaluationRunRow,
    EvaluationSuiteRow,
    ModelRow,
    ModelVariantRow,
    TrainingAdapterRow,
    TrainingAttemptRow,
    TrainingCheckpointRow,
    TrainingEvaluationTriggerRow,
    TrainingJobRow,
)
from coire_api.evaluation.catalog import build_suite
from coire_api.evaluation.evidence import persist_collected
from coire_api.evaluation.training import ensure_final_trigger, reconcile_final_trigger
from coire_api.training.service import payload_digest
from coire_core.models.acquisition import VariantState
from coire_core.models.datasets import TokenizedTrainingExample
from coire_core.models.evaluation import (
    EvaluationOutput,
    EvaluationSubject,
    EvaluationSuiteRegistration,
    EvaluationTarget,
    EvaluationWorkload,
)
from coire_core.models.harness import TaskClass
from coire_core.models.registry import ModelState
from coire_core.models.runs import AgentRunState
from coire_core.models.training import parse_resolved_training_spec
from coire_core.models.training_node import (
    CheckpointCommitAcknowledgementV2,
    EvaluationCheckpointPause,
    TrainingAdapterExtractRequest,
    TrainingArtifactManifest,
    TrainingPrepareRequest,
)
from coire_core.settings import Settings
from coire_node.testing.harness import Agent
from coire_node.training.artifacts import TrainingArtifacts
from coire_node.training.checkpoints import CheckpointStore
from coire_node.training.extraction import AdapterExtractor
from coire_node.training.objectives import load_sft_runtime, validate_sft_input
from coire_node.training.sampler import SingleSourceSampler
from coire_node.training.worker import (
    PausedAtCheckpoint,
    make_optimizer,
    restore_for_attempt,
    run_sft,
)
from coire_scheduler.evaluations import advance

pytestmark = [pytest.mark.engine, pytest.mark.integration]
__all__ = ["evaluation_engine"]


async def train_and_extract(
    agent: Agent,
    model: Path,
    job: TrainingJobRow,
    adapter_id: uuid.UUID,
    attempt_id: str,
    base_workload: EvaluationWorkload,
) -> TrainingArtifactManifest:
    resolved = parse_resolved_training_spec(job.resolved_spec)
    digest = job.resolved_sha256
    assert digest is not None
    command = TrainingPrepareRequest(
        command_id=uuid.uuid4(),
        job_id=job.id,
        attempt_id=attempt_id,
        fence=job.fence,
        request_sha256=digest,
        node="coire-edge-a",
        rank=0,
        world_size=1,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=29),
        reservation_id=uuid.uuid4(),
        disk_reservation_id=uuid.uuid4(),
        resolved=resolved,
    )
    source = validate_sft_input(model, resolved.spec.parameterization)
    runtime = load_sft_runtime(source, seed=42)
    optimizer = make_optimizer(resolved.spec.optim)
    examples = [
        TokenizedTrainingExample(
            source_row=i + 1,
            content_sha256=hashlib.sha256(str(i).encode()).hexdigest(),
            tokens=[1, 2 + i, 8, 9],
            target_mask=[False, True, True, True],
            target_start=1,
        )
        for i in range(4)
    ]

    def sampler(digest: str) -> SingleSourceSampler:
        return SingleSourceSampler(
            examples,
            dataset_sha256=digest * 64,
            batch_size=1,
            seed=7,
            max_sequence_length=8,
        )

    artifacts = TrainingArtifacts(
        Path(agent.settings.node_state_dir) / "training/artifacts", node_name="coire-edge-a"
    )
    store = CheckpointStore(artifacts.root, disk_floor_bytes=0)
    committed: list[TrainingArtifactManifest] = []

    def commit(manifest: TrainingArtifactManifest) -> CheckpointCommitAcknowledgementV2:
        restored = store.restore(
            manifest.artifact_id,
            expected_runtime_sha256=resolved.runtime_sha256,
            expected_resolved_spec_sha256=digest,
        )
        assert restored.state.completed_update == int(optimizer.step.item())
        committed.append(manifest)
        assert manifest.update is not None
        return CheckpointCommitAcknowledgementV2(
            command_id=uuid.uuid4(),
            job_id=job.id,
            attempt_id=attempt_id,
            fence=job.fence,
            request_sha256=digest,
            node="coire-edge-a",
            rank=0,
            world_size=1,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=29),
            checkpoint_id=manifest.artifact_id,
            manifest_sha256=manifest.canonical_sha256(),
            update=manifest.update,
            committed_update=manifest.update,
            job_version=job.version,
            evaluation_pause=EvaluationCheckpointPause(
                trigger_id=uuid.uuid4(), pause_command_id=uuid.uuid4()
            )
            if manifest.update == 2
            else None,
        )

    with pytest.raises(PausedAtCheckpoint) as paused:
        run_sft(
            command,
            runtime,
            optimizer,
            sampler("a"),
            sampler("b"),
            store=store,
            scratch=artifacts.root.parent / "scratch",
            emit=lambda event: None,
            commit=commit,
            control=lambda: "continue",
            footprint=lambda: 0,
        )
    assert paused.value.reason == "evaluation_pending"
    assert int(optimizer.step.item()) == 2 and [item.update for item in committed] == [2]
    checkpoint = committed[0]
    extractor = AdapterExtractor(artifacts, agent.reservations, agent.settings, agent.store)

    def extract(
        checkpoint: TrainingArtifactManifest, identity: uuid.UUID
    ) -> TrainingArtifactManifest:
        extraction = extractor.extract(
            TrainingAdapterExtractRequest(
                command_id=uuid.uuid4(),
                adapter_id=identity,
                checkpoint_id=checkpoint.artifact_id,
                checkpoint_manifest_sha256=checkpoint.canonical_sha256(),
                job_id=job.id,
                attempt_id=attempt_id,
                fence=job.fence,
                node="coire-edge-a",
                resolved=resolved,
                disk_reservation_id=uuid.uuid4(),
                max_bytes=64 * 1024**2,
                deadline=datetime.now(UTC) + timedelta(minutes=2),
            )
        )
        assert extraction.state == "succeeded" and extraction.manifest is not None
        assert extraction.manifest.resolved_spec_sha256 == digest
        return extraction.manifest

    private_adapter = extract(checkpoint, uuid.uuid4())
    checkpoint_work = base_workload.model_copy(
        update={
            "attempt_id": uuid.uuid4(),
            "run_id": uuid.uuid4(),
            "target": base_workload.target.model_copy(
                update={
                    "public_selector": f"{job.model_id}@checkpoint-isolated",
                    "target": base_workload.target.target.model_copy(
                        update={
                            "adapter_id": private_adapter.artifact_id,
                            "adapter_manifest_sha256": private_adapter.canonical_sha256(),
                        }
                    ),
                }
            ),
        }
    )
    checkpoint_work = EvaluationWorkload.model_validate(checkpoint_work.model_dump(mode="json"))
    _, measured = await phase(agent, model, "task-coding-instructions", workload=checkpoint_work)
    assert measured.outcome == "succeeded" and len(measured.outputs) == 16
    assert measured.target.adapter_id == private_adapter.artifact_id
    # The old trainer is discarded. Only the selected full-state checkpoint can
    # initialize a fresh runtime, optimizer and sampler; a different seed exposes
    # failures to restore the serialized RNG rather than relying on initialization.
    del runtime, optimizer
    runtime = load_sft_runtime(source, seed=999)
    optimizer = make_optimizer(resolved.spec.optim)
    training = sampler("a")
    command = command.model_copy(
        update={
            "resume_checkpoint_id": checkpoint.artifact_id,
            "resume_manifest_sha256": checkpoint.canonical_sha256(),
        }
    )
    completed = restore_for_attempt(command, runtime, optimizer, training, store)
    assert completed == 2 and int(optimizer.step.item()) == 2
    assert (
        run_sft(
            command,
            runtime,
            optimizer,
            training,
            sampler("b"),
            store=store,
            scratch=artifacts.root.parent / "resumed-scratch",
            completed_update=completed,
            emit=lambda event: None,
            commit=commit,
            control=lambda: "continue",
            footprint=lambda: 0,
        )
        == 4
    )
    assert [item.update for item in committed] == [2, 4]
    extract(committed[-1], adapter_id)
    return committed[-1]


async def register_target(session: AsyncSession, target: EvaluationTarget) -> None:
    session.add(
        ModelRow(
            id=target.target.model_id,
            repo_id=f"isolated/{target.target.model_id}",
            slug=str(target.target.model_id),
            display_name="Isolated acquired fixture",
            state=ModelState.READY,
            visibility="admin_only",
            placement_policy="single:auto",
            memory_estimate_bytes=2 * 1024**3,
            idle_ttl_seconds=900,
            precision="4bit",
            weight_bytes=1,
            total_bytes=1,
            file_count=1,
        )
    )
    await session.flush()
    session.add(
        ModelVariantRow(
            id=target.target.variant_id,
            model_id=target.target.model_id,
            name=target.variant_slug,
            slug=target.variant_slug,
            precision="4bit",
            source_revision="isolated-acquired",
            memory_estimate_bytes=2 * 1024**3,
            state=VariantState.READY,
            validated=True,
            harness_verified=False,
        )
    )
    await session.flush()


async def test_real_training_creates_one_automatic_task_rubric_group_after_restart(
    evaluation_engine: tuple[Agent, Path, Path],
    training_postgres_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, candidate_model, judge_model = evaluation_engine
    base_work, _ = await phase(agent, candidate_model, "task-coding-instructions")
    judge_work, _ = await phase(agent, judge_model, "task-coding-instructions")
    base, judge = base_work.target, judge_work.target
    assert base.target.base_manifest_sha256 != judge.target.base_manifest_sha256
    monkeypatch.setattr("coire_scheduler.evaluations.reserve_phase", AsyncMock(return_value=False))
    settings = Settings(evaluations_enabled=True, training_dataset_dir=str(tmp_path))
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job_id, adapter_id = await seed_evaluated_training(session)
            await register_target(session, base)
            await register_target(session, judge)
            job = await session.get(TrainingJobRow, job_id)
            adapter = await session.get(TrainingAdapterRow, adapter_id)
            attempt = await session.scalar(select(TrainingAttemptRow))
            assert job is not None and adapter is not None and attempt is not None
            value: dict[str, Any] | None = copy.deepcopy(job.resolved_spec)
            assert value is not None
            value["evaluation_base"] = base.model_dump(mode="json")
            value["base_manifest_sha256"] = base.target.base_manifest_sha256
            value["tokenizer_sha256"] = base.runtime.tokenizer_sha256
            value["template_sha256"] = base.runtime.template_sha256
            value["runtime_sha256"] = base.runtime.runtime_sha256
            value["spec"]["model"] = {
                "model_id": str(base.target.model_id),
                "variant_id": str(base.target.variant_id),
            }
            value["spec"]["parameterization"].update(
                kind="qlora",
                rank=2,
                dropout=0.15,
                num_layers=1,
                target_modules=["self_attn.q_proj", "self_attn.v_proj"],
            )
            value["spec"]["optim"].update(
                updates=4,
                accumulation_steps=2,
                batch_size=1,
                max_sequence_length=8,
                learning_rate=0.001,
                schedule={"kind": "warmup_linear", "warmup_updates": 2},
            )
            value["spec"]["eval"]["loss_every_updates"] = 2
            value["spec"]["output"]["checkpoint_every_updates"] = 2
            value["spec"]["eval"]["suites"][0]["checkpoint_updates"] = [2]
            for index, declaration in enumerate(value["evaluations"]):
                declaration["schedule"] = value["spec"]["eval"]["suites"][index]
                old = declaration["suite"]
                definition = build_suite(
                    EvaluationSuiteRegistration(
                        suite_id=old["suite_id"],
                        version=1,
                        template_id=old["template"]["template_id"],
                        judge=EvaluationSubject(
                            model_id=judge.target.model_id, variant_id=judge.target.variant_id
                        )
                        if index
                        else None,
                    ),
                    owner=job.owner_user_id,
                    judge=judge if index else None,
                    now=datetime.now(UTC),
                )
                declaration["suite"] = definition.model_dump(mode="json")
                row = await session.scalar(
                    select(EvaluationSuiteRow).where(
                        EvaluationSuiteRow.suite_id == definition.suite_id
                    )
                )
                assert row is not None
                row.definition = definition.model_dump(mode="json")
                row.content_sha256 = definition.content_sha256
            resolved = parse_resolved_training_spec(value)
            job.model_id, job.base_variant_id = base.target.model_id, base.target.variant_id
            job.resolved_spec, job.resolved_sha256 = (
                resolved.model_dump(mode="json"),
                payload_digest(resolved),
            )
            job.completed_update = 4
            adapter.model_id, adapter.base_variant_id = job.model_id, job.base_variant_id
            adapter.base_manifest_sha256 = resolved.base_manifest_sha256
            adapter.resolved_spec_sha256, adapter.parameterization = job.resolved_sha256, "qlora"
            adapter.selector = f"{job.model_id}@{adapter.slug}"
            checkpoint = await train_and_extract(
                agent, candidate_model, job, adapter_id, attempt.id, base_work
            )
            session.add(
                TrainingCheckpointRow(
                    id=checkpoint.artifact_id,
                    job_id=job.id,
                    attempt_id=attempt.id,
                    fence=job.fence,
                    completed_update=4,
                    manifest_sha256=checkpoint.canonical_sha256(),
                    manifest=checkpoint.model_dump(mode="json"),
                    total_bytes=checkpoint.total_bytes,
                    state="committed",
                    committed_at=datetime.now(UTC),
                )
            )
            await session.flush()
            adapter.source_checkpoint_id = checkpoint.artifact_id
            artifact = TrainingArtifacts(
                Path(agent.settings.node_state_dir) / "training/artifacts", node_name="coire-edge-a"
            ).manifest(adapter_id)
            adapter.manifest_sha256 = artifact.canonical_sha256()
            trigger = await ensure_final_trigger(session, job, adapter, settings=settings)
            assert trigger is not None
            trigger_id = trigger.id
            await session.commit()
        for _ in range(2):
            async with AsyncSession(engine, expire_on_commit=False) as recovered:
                assert not await reconcile_final_trigger(recovered, trigger_id, settings=settings)
                await recovered.commit()
        async with AsyncSession(engine) as session:
            trigger = await session.get(TrainingEvaluationTriggerRow, trigger_id)
            assert trigger is not None
            runs = list(
                (
                    await session.scalars(
                        select(EvaluationRunRow).where(
                            EvaluationRunRow.group_id == trigger.group_id
                        )
                    )
                ).all()
            )
            assert len(runs) == 2
            run_ids = [run.id for run in runs]
            group = await session.get(EvaluationGroupRow, trigger.group_id)
            assert group is not None and group.origin == "training_final"
        for run_id in run_ids:
            outputs: list[EvaluationOutput] = []
            for _ in range(3):
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    if await advance(session, run_id, settings):
                        await session.commit()
                        break
                    await session.commit()
                    run = await session.get(EvaluationRunRow, run_id)
                    attempt = await session.scalar(
                        select(EvaluationAttemptRow)
                        .where(EvaluationAttemptRow.run_id == run_id)
                        .order_by(EvaluationAttemptRow.ordinal.desc())
                    )
                    assert run is not None and attempt is not None
                    work = EvaluationWorkload.model_validate(attempt.workload)
                    run.state, run.started_at, run.started_deadline_at = (
                        "running",
                        datetime.now(UTC),
                        work.deadline,
                    )
                    child = AgentRunRow(
                        id=work.run_id,
                        requester_user_id=run.owner_user_id,
                        purpose="evaluation",
                        evaluation_attempt_id=attempt.id,
                        profile="general",
                        primary_model_id=work.target.target.model_id,
                        primary_variant_id=work.target.target.variant_id,
                        workspace_ref=f"eval-{work.run_id}",
                        task_class=TaskClass.READ,
                        token_scope={},
                        state=AgentRunState.SUCCEEDED,
                        limits={},
                    )
                    session.add(child)
                    await session.flush()
                    attempt.agent_run_id = child.id
                    _, worker = await phase(
                        agent,
                        judge_model if work.phase == "judge" else candidate_model,
                        work.suite.template.template_id,
                        workload=work,
                        previous=outputs if work.phase == "judge" else None,
                    )
                    outputs.extend(worker.outputs)
                    receipt = await persist_collected(session, attempt.id, worker, settings)
                    await session.commit()
                async with AsyncSession(engine, expire_on_commit=False) as recovered:
                    assert (
                        await persist_collected(recovered, work.attempt_id, worker, settings)
                        == receipt
                    )
                    await recovered.commit()
                if worker.outcome == "failed":
                    break
            async with AsyncSession(engine, expire_on_commit=False) as session:
                assert await advance(session, run_id, settings)
                await session.commit()
                result = await session.scalar(
                    select(EvaluationResultRow).where(EvaluationResultRow.run_id == run_id)
                )
                assert result is not None
                assert result.result["harness_verdict"] is None
                digest = result.result_sha256
                assert await advance(session, run_id, settings)
                assert result.result_sha256 == digest
                if result.result["outcome"] == "failed":
                    assert result.result["reason"] == "malformed_judge"
                    assert result.result["aggregates"] == [None, None]
        async with AsyncSession(engine) as session:
            job = await session.get(TrainingJobRow, job_id)
            adapter = await session.get(TrainingAdapterRow, adapter_id)
            assert job is not None and job.state == "succeeded"
            assert adapter is not None and adapter.state == "ready" and not adapter.verified
            triggers = (await session.scalars(select(TrainingEvaluationTriggerRow))).all()
            assert len(triggers) == 1
    finally:
        await engine.dispose()
