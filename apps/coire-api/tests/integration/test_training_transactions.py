"""Actual PostgreSQL fences, all-copy commits and cancellation races."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    Base,
    MemoryReservationRow,
    ModelRow,
    ModelVariantRow,
    NodeRow,
    TrainingAttemptRow,
    TrainingCheckpointRow,
    TrainingJobRow,
    TrainingParticipantRow,
    UserRow,
)
from coire_api.training.checkpoints import (
    commit_checkpoint,
    record_verified_copy,
    recovery_checkpoint,
    stage_checkpoint,
)
from coire_api.training.service import payload_digest, submit_training
from coire_core.errors import TrainingConflict
from coire_core.models.auth import UserRole
from coire_core.models.datasets import DatasetAnalysis, SplitManifest
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.training import ResolvedTrainingSpec, TrainingSpec, TrainingSubmission
from coire_core.models.training_node import (
    TrainingArtifactManifest,
    TrainingArtifactVerificationReceipt,
    TrainingStopReceipt,
)
from coire_scheduler.training_recovery import record_stop_proof

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="requires disposable Postgres: COIRE_INTEGRATION=1",
    ),
]
JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ATTEMPT = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
DIGEST = "a" * 64


@pytest.fixture
async def database(training_postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(training_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        owner, model, variant = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await conn.execute(
            insert(UserRow).values(
                id=owner,
                email=f"{owner}@training.test",
                display_name="admin",
                role="admin",
                active=True,
            )
        )
        await conn.execute(
            insert(ModelRow).values(
                id=model,
                repo_id="synthetic/test",
                slug="test",
                display_name="test",
                state="ready",
                visibility="admin_only",
                placement_policy="single:auto",
                memory_estimate_bytes=100,
                idle_ttl_seconds=900,
                precision="bf16",
                weight_bytes=100,
                total_bytes=100,
                file_count=1,
            )
        )
        await conn.execute(
            insert(ModelVariantRow).values(
                id=variant,
                model_id=model,
                name="test",
                slug="test-variant",
                precision="bf16",
                state="ready",
                source_revision="fixture",
            )
        )
        now = datetime.now(UTC)
        await conn.execute(
            insert(TrainingJobRow).values(
                id=JOB,
                owner_user_id=owner,
                model_id=model,
                base_variant_id=variant,
                idempotency_key="test",
                output_slug="test",
                source_yaml="{}",
                source_sha256=DIGEST,
                intent_sha256=DIGEST,
                submitted_spec={},
                resolved_sha256=DIGEST,
                state="running",
                fence=1,
                queue_deadline_at=now + timedelta(hours=1),
                execution_deadline_at=now + timedelta(hours=1),
            )
        )
        await conn.execute(
            insert(TrainingAttemptRow).values(
                id=ATTEMPT,
                job_id=JOB,
                generation=1,
                fence=1,
                world_size=1,
                runtime_sha256=DIGEST,
                state="running",
                lease_expires_at=now + timedelta(minutes=10),
            )
        )
        for name in ("coire-edge-a", "coire-edge-b"):
            await conn.execute(
                insert(NodeRow).values(
                    id=uuid.uuid4(),
                    name=name,
                    role="studio",
                    memory_total_bytes=10000,
                    disk_total_bytes=10000,
                    agent_version="fixture",
                )
            )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def manifest(update: int = 1) -> TrainingArtifactManifest:
    return TrainingArtifactManifest.model_validate(
        {
            "artifact_id": str(uuid.uuid4()),
            "kind": "checkpoint",
            "job_id": JOB,
            "attempt_id": ATTEMPT,
            "fence": 1,
            "update": update,
            "world_size": 1,
            "runtime_sha256": DIGEST,
            "resolved_spec_sha256": DIGEST,
            "total_bytes": 3,
            "files": [
                {"id": name, "name": name + suffix, "bytes": 1, "sha256": DIGEST}
                for name, suffix in (
                    ("adapter", ".safetensors"),
                    ("optimizer", ".safetensors"),
                    ("state", ".json"),
                )
            ],
            "ranks": [
                {
                    "rank": 0,
                    "update": update,
                    "adapter_file_id": "adapter",
                    "optimizer_file_id": "optimizer",
                    "state_file_id": "state",
                    "adapter_tensors": [{"key": "a", "shape": [1], "dtype": "float32"}],
                    "optimizer_tensors": [{"key": "m", "shape": [1], "dtype": "float32"}],
                }
            ],
        }
    )


async def mirrored(
    factory: async_sessionmaker[AsyncSession], item: TrainingArtifactManifest
) -> None:
    async with factory.begin() as session:
        await stage_checkpoint(session, item)
        for node in ("coire-edge-a", "coire-edge-b"):
            proof = TrainingArtifactVerificationReceipt.model_validate(
                {
                    "command_id": str(uuid.uuid4()),
                    "artifact_id": item.artifact_id,
                    "manifest_sha256": item.canonical_sha256(),
                    "node": node,
                    "verified_bytes": item.total_bytes,
                }
            )
            await record_verified_copy(session, item, proof, checkpoint_id=item.artifact_id)


async def test_checkpoint_requires_both_complete_copies_and_replays_once(
    database: async_sessionmaker[AsyncSession],
) -> None:
    item = manifest()
    async with database.begin() as session:
        await stage_checkpoint(session, item)
    async with database.begin() as session:
        with pytest.raises(TrainingConflict, match="both independently"):
            await commit_checkpoint(session, item.artifact_id)
    await mirrored(database, item)

    async def commit() -> str:
        async with database.begin() as session:
            return (await commit_checkpoint(session, item.artifact_id)).state

    outcomes = await asyncio.gather(commit(), commit())
    assert len(outcomes) == 2 and set(outcomes) == {"committed"}
    async with database() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert (
            job is not None
            and job.latest_checkpoint_id == item.artifact_id
            and job.next_event_sequence == 2
        )


async def test_cancel_lock_prevents_late_checkpoint_publication(
    database: async_sessionmaker[AsyncSession],
) -> None:
    item = manifest()
    await mirrored(database, item)
    acquired, release = asyncio.Event(), asyncio.Event()

    async def cancel() -> None:
        async with database.begin() as session:
            job = await session.get(TrainingJobRow, JOB, with_for_update=True)
            assert job is not None
            job.state = "cancelling"
            acquired.set()
            await release.wait()

    async def commit() -> None:
        await acquired.wait()
        async with database.begin() as session:
            await commit_checkpoint(session, item.artifact_id)

    cancel_task = asyncio.create_task(cancel())
    commit_task = asyncio.create_task(commit())
    await acquired.wait()
    await asyncio.sleep(0.05)
    assert not commit_task.done()
    release.set()
    await cancel_task
    with pytest.raises(TrainingConflict, match="live attempt"):
        await commit_task


async def test_corrupt_newest_checkpoint_falls_back(
    database: async_sessionmaker[AsyncSession],
) -> None:
    older, newer = manifest(1), manifest(2)
    for item in (older, newer):
        await mirrored(database, item)
        async with database.begin() as session:
            await commit_checkpoint(session, item.artifact_id)
    async with database.begin() as session:
        row = await session.get(TrainingCheckpointRow, newer.artifact_id)
        assert row is not None
        row.manifest = {**row.manifest, "total_bytes": 100}
    async with database.begin() as session:
        job = await session.get(TrainingJobRow, JOB, with_for_update=True)
        assert job is not None
        selected = await recovery_checkpoint(session, job)
        assert selected is not None and selected.id == older.artifact_id
        row = await session.get(TrainingCheckpointRow, newer.artifact_id)
        assert row is not None and row.state == "corrupt"


async def test_release_requires_matching_process_proof(
    database: async_sessionmaker[AsyncSession],
) -> None:
    async with database.begin() as session:
        node = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-a"))
        assert node is not None
        hold = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node.id,
            holder_type=ReservationHolder.TRAINING,
            holder_id=ATTEMPT,
            bytes=100,
            pinned=True,
            state=MemoryReservationState.HELD,
        )
        session.add(hold)
        await session.flush()
        session.add(
            TrainingParticipantRow(
                attempt_id=ATTEMPT,
                node_id=node.id,
                rank=0,
                reservation_id=hold.id,
                command_id=uuid.uuid4(),
                request_sha256=DIGEST,
                spawn_nonce=uuid.uuid4(),
                pid=123,
                process_create_time=12.0,
            )
        )
        hold_id = hold.id
    proof = TrainingStopReceipt(
        attempt_id=ATTEMPT,
        fence=1,
        node="coire-edge-a",
        pid=123,
        process_create_time=13.0,
        stopped=True,
        observed_at=datetime.now(UTC),
    )
    async with database.begin() as session:
        with pytest.raises(TrainingConflict, match="owned process"):
            await record_stop_proof(session, ATTEMPT, proof)
    async with database() as session:
        observed_hold = await session.get(MemoryReservationRow, hold_id)
        assert observed_hold is not None and observed_hold.state is MemoryReservationState.HELD
    proof = proof.model_copy(update={"process_create_time": 12.0})
    async with database.begin() as session:
        assert await record_stop_proof(session, ATTEMPT, proof)
    async with database() as session:
        observed_hold = await session.get(MemoryReservationRow, hold_id)
        assert observed_hold is not None and observed_hold.state is MemoryReservationState.RELEASED
        released_at = observed_hold.released_at
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        version, state = job.version, job.state
    async with database.begin() as session:
        assert await record_stop_proof(session, ATTEMPT, proof)
    async with database() as session:
        observed_hold = await session.get(MemoryReservationRow, hold_id)
        job = await session.get(TrainingJobRow, JOB)
        assert observed_hold is not None and observed_hold.released_at == released_at
        assert job is not None and (job.version, job.state) == (version, state)


async def bound_submission(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[Principal, TrainingSubmission, ResolvedTrainingSpec]:
    from coire_api.db import (
        TrainingCommandRow,
        TrainingDatasetAnalysisRow,
        TrainingDatasetRevisionRow,
        VariantCopyRow,
    )
    from coire_core.models.datasets import DatasetAnalysisBinding
    from coire_core.models.training import ResolvedDatasetInput, TrainingResourceEnvelope

    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        dataset_id, analysis_id = uuid.uuid4(), uuid.uuid4()
        split = SplitManifest(
            dataset_id=dataset_id,
            source_sha256=DIGEST,
            seed=0,
            train_rows=[1],
            validation_rows=[2],
            row_content_sha256=["b" * 64, "c" * 64],
        )
        source = TrainingDatasetRevisionRow(
            id=dataset_id,
            owner_user_id=job.owner_user_id,
            name="race-input",
            format="text",
            state="ready",
            source_sha256=DIGEST,
            source_bytes=10,
            storage_key=str(dataset_id),
            provenance={"source": "fixture", "license_note": "test"},
            row_count=2,
            split_seed=0,
            validation_fraction=0.5,
            split_manifest=split.model_dump(mode="json"),
            split_sha256=payload_digest(split),
        )
        session.add(source)
        await session.flush()
        analysis = DatasetAnalysis.model_validate(
            {
                "id": analysis_id,
                "dataset_id": dataset_id,
                "model_id": job.model_id,
                "variant_id": job.base_variant_id,
                "state": "succeeded",
                "tokenizer_sha256": DIGEST,
                "template_sha256": DIGEST,
                "runtime_sha256": DIGEST,
                "tokens": {
                    "minimum": 3,
                    "maximum": 3,
                    "p50": 3,
                    "p95": 3,
                    "histogram": [2],
                    "upper_bounds": [3],
                },
                "row_count": 2,
                "created_at": datetime.now(UTC),
            }
        )
        binding = DatasetAnalysisBinding.model_validate(
            {
                "dataset_id": dataset_id,
                "model_id": job.model_id,
                "variant_id": job.base_variant_id,
                "base_manifest_sha256": DIGEST,
                "source_sha256": DIGEST,
                "split_sha256": source.split_sha256,
                "format": "text",
                "model_slug": "test-variant",
            }
        )
        analysis_command = uuid.uuid4()
        session.add(
            TrainingCommandRow(
                id=analysis_command,
                actor_user_id=job.owner_user_id,
                idempotency_key=f"fixture-analysis:{dataset_id}",
                operation="dataset.analyze",
                subject_id=str(dataset_id),
                request_sha256=payload_digest(binding),
                payload={"analysis": binding.model_dump(mode="json")},
                state="succeeded",
            )
        )
        session.add(
            TrainingDatasetAnalysisRow(
                id=analysis_id,
                dataset_id=dataset_id,
                model_id=job.model_id,
                variant_id=job.base_variant_id,
                command_id=analysis_command,
                identity_sha256=payload_digest(binding),
                tokenizer_sha256=DIGEST,
                template_sha256=DIGEST,
                runtime_sha256=DIGEST,
                state="succeeded",
                result=analysis.model_dump(mode="json"),
            )
        )
        nodes = list((await session.scalars(select(NodeRow))).all())
        for index, node in enumerate(nodes):
            session.add(
                VariantCopyRow(
                    variant_id=job.base_variant_id,
                    node_id=node.id,
                    path="/synthetic",
                    bytes=100,
                    manifest_sha256=DIGEST,
                    verified=True,
                    role="origin" if index == 0 else "replica",
                )
            )
        spec = TrainingSpec.model_validate(
            {
                "model": {"model_id": job.model_id, "variant_id": job.base_variant_id},
                "data": {
                    "loss_policy": "all_tokens",
                    "train": {
                        "datasets": [
                            {"dataset_id": dataset_id, "sample_count": 1, "mixture_proportion": 1.0}
                        ],
                        "epoch_samples": 1,
                    },
                    "validation": {"dataset_ids": [dataset_id]},
                },
                "parameterization": {"target_modules": ["self_attn.q_proj"]},
                "optim": {"updates": 2},
                "output": {"adapter_slug": "race-output"},
            }
        )
        resolved = ResolvedTrainingSpec(
            spec=spec,
            base_manifest_sha256=DIGEST,
            datasets=[
                ResolvedDatasetInput.model_validate(
                    {
                        "dataset_id": dataset_id,
                        "analysis_id": analysis_id,
                        "source_sha256": DIGEST,
                        "split_sha256": source.split_sha256,
                        "analysis_sha256": payload_digest(analysis),
                    }
                )
            ],
            tokenizer_sha256=DIGEST,
            template_sha256=DIGEST,
            enable_thinking=False,
            runtime_sha256=DIGEST,
            worker_version="fixture",
            resource_envelope=TrainingResourceEnvelope.model_validate(
                {
                    "weight_bytes": 100,
                    "adapter_bytes": 10,
                    "optimizer_bytes": 20,
                    "activation_bytes": 30,
                    "buffer_bytes": 0,
                    "safety_bytes": 10,
                    "checkpoint_bytes": 20,
                    "evidence_sha256": DIGEST,
                }
            ),
        )
        principal = Principal(
            kind=PrincipalKind.USER,
            subject=str(job.owner_user_id),
            user_id=job.owner_user_id,
            role=UserRole.ADMIN,
        )
        return (
            principal,
            TrainingSubmission(source_yaml=yaml.safe_dump(spec.model_dump(mode="json"))),
            resolved,
        )


async def test_concurrent_submission_replays_one_job_and_one_input_pin(
    database: async_sessionmaker[AsyncSession],
) -> None:
    from coire_api.db import TrainingDatasetReferenceRow

    principal, body, resolved = await bound_submission(database)

    async def submit() -> str:
        async with database.begin() as session:
            return (
                await submit_training(session, principal, body, "same", resolved=resolved)
            ).job_id

    first, second = await asyncio.gather(submit(), submit())
    assert first == second
    async with database() as session:
        references = list(
            (
                await session.scalars(
                    select(TrainingDatasetReferenceRow).where(
                        TrainingDatasetReferenceRow.job_id == first
                    )
                )
            ).all()
        )
        assert len(references) == 1


async def test_source_retirement_serializes_with_submission(
    database: async_sessionmaker[AsyncSession],
) -> None:
    from coire_api.db import TrainingDatasetReferenceRow, TrainingDatasetRevisionRow

    principal, body, resolved = await bound_submission(database)
    acquired, release = asyncio.Event(), asyncio.Event()

    async def retire() -> None:
        async with database.begin() as session:
            source = await session.get(
                TrainingDatasetRevisionRow,
                resolved.datasets[0].dataset_id,
                populate_existing=True,
                with_for_update=True,
            )
            assert source is not None
            source.state = "retired"
            acquired.set()
            await release.wait()

    async def submit() -> None:
        await acquired.wait()
        async with database.begin() as session:
            await submit_training(session, principal, body, "retirement-race", resolved=resolved)

    retirement, submission = asyncio.create_task(retire()), asyncio.create_task(submit())
    await acquired.wait()
    await asyncio.sleep(0.05)
    assert not submission.done()
    release.set()
    await retirement
    with pytest.raises(TrainingConflict, match="unavailable"):
        await submission
    async with database() as session:
        assert await session.scalar(select(TrainingDatasetReferenceRow.job_id)) is None


async def test_competing_admissions_count_the_full_envelope_once(
    database: async_sessionmaker[AsyncSession],
) -> None:
    from coire_api.db import NodeMemoryLedgerRow
    from coire_scheduler.training_admission import admit_training

    principal, body, resolved = await bound_submission(database)
    async with database.begin() as session:
        from training_measurement_fixtures import persist_measured_profile

        node = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-a"))
        assert node is not None and principal.user_id is not None
        resolved = await persist_measured_profile(session, principal.user_id, resolved, [node])
        first = await submit_training(session, principal, body, "first", resolved=resolved)
        second_spec = resolved.spec.model_copy(
            update={
                "output": resolved.spec.output.model_copy(update={"adapter_slug": "second-output"})
            }
        )
        second_resolved = resolved.model_copy(update={"spec": second_spec})
        second_body = TrainingSubmission(
            source_yaml=yaml.safe_dump(second_spec.model_dump(mode="json"))
        )
        second = await submit_training(
            session, principal, second_body, "second", resolved=second_resolved
        )
        node = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-a"))
        assert node is not None
        node_id = node.id
        session.add(
            NodeMemoryLedgerRow(
                node_id=node_id,
                budget_bytes=1000,
                sandbox_bytes=0,
                measured_resident_bytes=0,
                thermal_state="nominal",
                health="healthy",
                health_sampled_at=datetime.now(UTC),
            )
        )

    async def admit(job_id: str) -> str | None:
        async with database.begin() as session:
            job = await session.get(TrainingJobRow, job_id)
            node = await session.get(NodeRow, node_id)
            assert job is not None and node is not None
            attempt = await admit_training(session, job, [node])
            return attempt.id if attempt else None

    outcomes = await asyncio.gather(admit(first.job_id), admit(second.job_id))
    assert sum(value is not None for value in outcomes) == 1
    async with database() as session:
        holds = list(
            (
                await session.scalars(
                    select(MemoryReservationRow).where(
                        MemoryReservationRow.holder_type == ReservationHolder.TRAINING
                    )
                )
            ).all()
        )
        assert len(holds) == 1 and holds[0].bytes == resolved.resource_envelope.memory_bytes == 170
        assert holds[0].state is MemoryReservationState.PENDING


async def test_cancelled_late_start_is_tracked_for_teardown_not_running(
    database: async_sessionmaker[AsyncSession],
) -> None:
    from coire_api.db import TrainingCommandRow
    from coire_core.models.training_node import TrainingStartReceipt
    from coire_scheduler.training import reduce_command_receipt

    command_id = uuid.uuid4()
    async with database.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        node = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-a"))
        assert job is not None and node is not None
        job.state = "cancelling"
        hold = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node.id,
            holder_type=ReservationHolder.TRAINING,
            holder_id=ATTEMPT,
            bytes=100,
            pinned=True,
            state=MemoryReservationState.PENDING,
        )
        session.add(hold)
        await session.flush()
        session.add(
            TrainingParticipantRow(
                attempt_id=ATTEMPT,
                node_id=node.id,
                rank=0,
                reservation_id=hold.id,
                command_id=uuid.uuid4(),
                request_sha256=DIGEST,
                spawn_nonce=uuid.uuid4(),
            )
        )
        session.add(
            TrainingCommandRow(
                id=command_id,
                actor_user_id=job.owner_user_id,
                idempotency_key="late-start",
                operation="node.training.start",
                subject_id=node.name,
                job_id=JOB,
                attempt_id=ATTEMPT,
                payload={},
                request_sha256=DIGEST,
                state="dispatching",
            )
        )
        hold_id = hold.id
    async with database.begin() as session:
        await reduce_command_receipt(
            session,
            command_id,
            TrainingStartReceipt(
                attempt_id=ATTEMPT,
                fence=1,
                pid=777,
                process_create_time=10.0,
                reservation_id=hold_id,
            ),
        )
    async with database() as session:
        job, attempt = (
            await session.get(TrainingJobRow, JOB),
            await session.get(TrainingAttemptRow, ATTEMPT),
        )
        observed_hold = await session.get(MemoryReservationRow, hold_id)
        assert job is not None and job.state == "cancelling"
        assert attempt is not None and attempt.state == "stopping"
        assert observed_hold is not None and observed_hold.state is MemoryReservationState.HELD


async def test_adapter_cannot_be_ready_without_reserved_exact_smoke(
    database: async_sessionmaker[AsyncSession],
) -> None:
    from coire_api.db import TrainingAdapterRow
    from coire_api.training.adapters import finalize_serving_adapter, stage_serving_adapter
    from coire_core.models.engine import EngineState, EngineStatus

    principal, _, resolved = await bound_submission(database)
    async with database.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        job.resolved_spec = resolved.model_dump(mode="json")
        job.resolved_sha256 = payload_digest(resolved)
        job.authorization_snapshot = principal.model_dump(mode="json")
        job.output_slug = resolved.spec.output.adapter_slug
    checkpoint = manifest(2).model_copy(update={"resolved_spec_sha256": payload_digest(resolved)})
    await mirrored(database, checkpoint)
    async with database.begin() as session:
        await commit_checkpoint(session, checkpoint.artifact_id)
        job, attempt = (
            await session.get(TrainingJobRow, JOB),
            await session.get(TrainingAttemptRow, ATTEMPT),
        )
        assert job is not None and attempt is not None
        job.state = "recovering"
        attempt.state, attempt.stopped_at = "stopped", datetime.now(UTC)
    artifact = TrainingArtifactManifest.model_validate(
        {
            "artifact_id": str(uuid.uuid4()),
            "kind": "adapter",
            "job_id": JOB,
            "attempt_id": ATTEMPT,
            "fence": 1,
            "update": 2,
            "world_size": 1,
            "runtime_sha256": DIGEST,
            "resolved_spec_sha256": payload_digest(resolved),
            "total_bytes": 2,
            "files": [
                {"id": "weights", "name": "adapters.safetensors", "bytes": 1, "sha256": DIGEST},
                {"id": "config", "name": "adapter_config.json", "bytes": 1, "sha256": DIGEST},
            ],
        }
    )
    async with database.begin() as session:
        await stage_serving_adapter(
            session,
            principal,
            checkpoint.artifact_id,
            artifact,
            slug=resolved.spec.output.adapter_slug,
            automatic=True,
        )
        for node in ("coire-edge-a", "coire-edge-b"):
            proof = TrainingArtifactVerificationReceipt.model_validate(
                {
                    "command_id": str(uuid.uuid4()),
                    "artifact_id": artifact.artifact_id,
                    "manifest_sha256": artifact.canonical_sha256(),
                    "node": node,
                    "verified_bytes": 2,
                }
            )
            await record_verified_copy(session, artifact, proof, adapter_id=artifact.artifact_id)
    # A node-ready flag without an exact reserved dedicated instance is not smoke evidence.
    smoke = EngineStatus(
        engine_id=uuid.uuid4(),
        port=1234,
        pid=777,
        process_create_time=10.0,
        state=EngineState.READY,
        started_at=datetime.now(UTC),
        last_health_at=datetime.now(UTC),
    )
    async with database.begin() as session:
        with pytest.raises(TrainingConflict, match="actual reserved"):
            await finalize_serving_adapter(
                session, artifact.artifact_id, smoke, instance_id=uuid.uuid4()
            )
    async with database() as session:
        row = await session.get(TrainingAdapterRow, artifact.artifact_id)
        assert (
            row is not None
            and row.state == "validating"
            and row.visibility == "admin_only"
            and not row.verified
        )


async def test_admin_training_route_history_control_and_schema_are_typed(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from fastapi import FastAPI

    from coire_api import db
    from coire_api.routes.admin_training import router
    from coire_api.training.authorization import require_training_principal
    from coire_core.settings import Settings

    principal, body, resolved = await bound_submission(database)
    async with database.begin() as session:
        receipt = await submit_training(session, principal, body, "route-test", resolved=resolved)
        seeded = await session.get(TrainingJobRow, JOB)
        assert seeded is not None
        seeded.deleted_at = datetime.now(UTC)
    monkeypatch.setattr(db, "_sessionmaker", database)
    app = FastAPI()
    app.state.settings = Settings(training_enabled=True)
    app.include_router(router)
    app.dependency_overrides[require_training_principal] = lambda: principal
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        listing = await client.get("/api/v1/admin/training/jobs")
        assert listing.status_code == 200 and listing.json()["items"][0]["id"] == receipt.job_id
        detail = await client.get(f"/api/v1/admin/training/jobs/{receipt.job_id}")
        assert detail.status_code == 200 and detail.json()["source_yaml"] == body.source_yaml
        events = await client.get(f"/api/v1/admin/training/jobs/{receipt.job_id}/events")
        assert (
            events.status_code == 200 and events.json()["events"][0]["payload"]["state"] == "queued"
        )
        metrics = await client.get(f"/api/v1/admin/training/jobs/{receipt.job_id}/metrics")
        assert metrics.status_code == 200 and metrics.json()["items"] == []
        paused = await client.post(
            f"/api/v1/admin/training/jobs/{receipt.job_id}/pause",
            headers={"idempotency-key": "pause"},
            json={"expected_version": 1},
        )
        assert paused.status_code == 202 and paused.json()["state"] == "paused"
        replay = await client.post(
            f"/api/v1/admin/training/jobs/{receipt.job_id}/pause",
            headers={"idempotency-key": "pause"},
            json={"expected_version": 1},
        )
        assert replay.json() == paused.json()
        refused = await client.post(
            f"/api/v1/admin/training/jobs/{receipt.job_id}/pause",
            headers={"idempotency-key": "extra"},
            json={"expected_version": 2, "engine_path": "/caller"},
        )
        assert refused.status_code == 422
    assert "TrainingCommandReceipt" in app.openapi()["components"]["schemas"]


async def test_public_preflight_requires_current_binding_and_measured_envelope(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from coire_api import db
    from coire_api.db import TrainingCommandRow
    from coire_api.routes.admin_training import router
    from coire_api.training.authorization import require_training_principal
    from coire_core.errors import CoireError
    from coire_core.settings import Settings

    principal, body, resolved = await bound_submission(database)
    monkeypatch.setattr(db, "_sessionmaker", database)
    app = FastAPI()
    app.state.settings = Settings(training_enabled=True)
    app.include_router(router)
    app.dependency_overrides[require_training_principal] = lambda: principal

    @app.exception_handler(CoireError)
    async def problem(request: Request, error: CoireError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content={"detail": str(error)})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        validation = await client.post(
            "/api/v1/admin/training/validate", json=body.model_dump(mode="json")
        )
        assert validation.status_code == 200
        assert validation.json()["ready_to_run"] is False and validation.json()["reasons"] == [
            "profile_missing"
        ]
        refused = await client.post(
            "/api/v1/admin/training/jobs",
            headers={"idempotency-key": "no-evidence"},
            json=body.model_dump(mode="json"),
        )
        assert refused.status_code == 503
        async with database() as session:
            assert (
                await session.scalar(
                    select(TrainingCommandRow.id).where(
                        TrainingCommandRow.idempotency_key == "no-evidence"
                    )
                )
                is None
            )
        async with database.begin() as session:
            original = await submit_training(
                session, principal, body, "accepted-internal", resolved=resolved
            )
            model = await session.get(ModelRow, resolved.spec.model.model_id)
            assert model is not None
            model.chat_template = "changed effective template"
        pending = await client.post(
            "/api/v1/admin/training/validate", json=body.model_dump(mode="json")
        )
        assert pending.status_code == 200 and pending.json()["reasons"] == ["analysis_pending"]
        replay = await client.post(
            "/api/v1/admin/training/jobs",
            headers={"idempotency-key": "accepted-internal"},
            json=body.model_dump(mode="json"),
        )
        assert replay.status_code == 202 and replay.json()["job_id"] == original.job_id
        unsupported = resolved.spec.model_copy(
            update={
                "placement": resolved.spec.placement.model_copy(update={"mode": "data_parallel"})
            }
        )
        response = await client.post(
            "/api/v1/admin/training/validate",
            json={"source_yaml": yaml.safe_dump(unsupported.model_dump(mode="json"))},
        )
        assert response.status_code == 422


async def test_native_mailbox_replays_once_and_stopped_event_is_not_death_proof(
    database: async_sessionmaker[AsyncSession],
) -> None:
    from coire_api.db import TrainingMetricRow
    from coire_core.models.training import TrainingMetricSample
    from coire_core.models.training_node import (
        NodeControlPayload,
        NodeProgressPayload,
        NodeTrainingEvent,
    )
    from coire_scheduler.training import ingest_training_event

    async with database.begin() as session:
        node = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-a"))
        assert node is not None
        hold = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node.id,
            holder_type=ReservationHolder.TRAINING,
            holder_id=ATTEMPT,
            bytes=100,
            pinned=True,
            state=MemoryReservationState.HELD,
        )
        session.add(hold)
        await session.flush()
        session.add(
            TrainingParticipantRow(
                attempt_id=ATTEMPT,
                node_id=node.id,
                rank=0,
                reservation_id=hold.id,
                command_id=uuid.uuid4(),
                request_sha256=DIGEST,
                spawn_nonce=uuid.uuid4(),
                pid=777,
                process_create_time=datetime.now(UTC).timestamp(),
            )
        )
        hold_id = hold.id
    metric = TrainingMetricSample(
        job_id=JOB,
        attempt_id=ATTEMPT,
        update=1,
        kind="train",
        loss=1.0,
        learning_rate=0.01,
        tokens=3,
        tokens_per_second=3.0,
        updates_per_second=1.0,
        footprint_bytes=50,
        peak_bytes=50,
        recorded_at=datetime.now(UTC),
    )
    event = NodeTrainingEvent(
        sequence=1,
        job_id=JOB,
        attempt_id=ATTEMPT,
        fence=1,
        update=1,
        recorded_at=datetime.now(UTC),
        payload=NodeProgressPayload(metric=metric),
    )
    async with database.begin() as session:
        await ingest_training_event(session, "coire-edge-a", event)
        await ingest_training_event(session, "coire-edge-a", event)
    async with database() as session:
        assert len(list((await session.scalars(select(TrainingMetricRow))).all())) == 1
    changed = event.model_copy(
        update={"payload": NodeProgressPayload(metric=metric.model_copy(update={"loss": 2.0}))}
    )
    async with database.begin() as session:
        with pytest.raises(TrainingConflict, match="immutable"):
            await ingest_training_event(session, "coire-edge-a", changed)
    stopped = NodeTrainingEvent(
        sequence=2,
        job_id=JOB,
        attempt_id=ATTEMPT,
        fence=1,
        update=1,
        recorded_at=datetime.now(UTC),
        payload=NodeControlPayload(kind="stopped", reason="cancelled"),
    )
    async with database.begin() as session:
        await ingest_training_event(session, "coire-edge-a", stopped)
    async with database() as session:
        observed_hold = await session.get(MemoryReservationRow, hold_id)
        assert observed_hold is not None and observed_hold.state is MemoryReservationState.HELD


async def test_lost_start_ack_accepts_owned_stop_identity_only_with_persisted_spawn_nonce(
    database: async_sessionmaker[AsyncSession],
) -> None:
    from coire_api.db import TrainingCommandRow
    from coire_core.models.training_node import TrainingStartRequest

    async with database.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        node = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-a"))
        assert job is not None and node is not None
        job.state = "cancelling"
        hold = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node.id,
            holder_type=ReservationHolder.TRAINING,
            holder_id=ATTEMPT,
            bytes=100,
            pinned=True,
            state=MemoryReservationState.HELD,
        )
        session.add(hold)
        await session.flush()
        prepare_id, nonce, command_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        session.add(
            TrainingParticipantRow(
                attempt_id=ATTEMPT,
                node_id=node.id,
                rank=0,
                reservation_id=hold.id,
                command_id=prepare_id,
                request_sha256=DIGEST,
                spawn_nonce=nonce,
            )
        )
        request = TrainingStartRequest(
            command_id=command_id,
            job_id=JOB,
            attempt_id=ATTEMPT,
            fence=1,
            request_sha256=DIGEST,
            node="coire-edge-a",
            rank=0,
            world_size=1,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            prepared_command_id=prepare_id,
            spawn_nonce=nonce,
        )
        session.add(
            TrainingCommandRow(
                id=command_id,
                actor_user_id=job.owner_user_id,
                idempotency_key="lost-start",
                operation="node.training.start",
                subject_id=node.name,
                job_id=JOB,
                attempt_id=ATTEMPT,
                request_sha256=payload_digest(request),
                payload=request.model_dump(mode="json"),
                state="dispatching",
            )
        )
        hold_id = hold.id
    proof = TrainingStopReceipt(
        attempt_id=ATTEMPT,
        fence=1,
        node="coire-edge-a",
        pid=777,
        process_create_time=datetime.now(UTC).timestamp(),
        stopped=True,
        observed_at=datetime.now(UTC),
    )
    async with database.begin() as session:
        assert await record_stop_proof(session, ATTEMPT, proof)
    async with database() as session:
        observed_hold = await session.get(MemoryReservationRow, hold_id)
        job = await session.get(TrainingJobRow, JOB)
        assert observed_hold is not None and observed_hold.state is MemoryReservationState.RELEASED
        assert job is not None and job.state == "cancelled"


async def test_adapter_patch_and_post_retire_return_immutable_detail_with_delete_compatibility(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from coire_api import db
    from coire_api.db import TrainingAdapterRow
    from coire_api.routes.admin_adapters import router
    from coire_api.training.authorization import require_training_principal
    from coire_core.errors import CoireError
    from coire_core.settings import Settings

    item = manifest()
    adapter_id = uuid.uuid4()
    async with database.begin() as session:
        await stage_checkpoint(session, item)
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        principal = Principal(
            kind=PrincipalKind.USER, user_id=job.owner_user_id, role=UserRole.ADMIN
        )
        session.add(
            TrainingAdapterRow(
                id=adapter_id,
                model_id=job.model_id,
                base_variant_id=job.base_variant_id,
                source_job_id=JOB,
                source_checkpoint_id=item.artifact_id,
                slug="pending-curation",
                selector=f"{job.model_id}@pending-curation",
                base_manifest_sha256=DIGEST,
                resolved_spec_sha256=DIGEST,
                parameterization="lora",
                state="validating",
                visibility="admin_only",
                verified=False,
                version=1,
            )
        )
    monkeypatch.setattr(db, "_sessionmaker", database)
    app = FastAPI()
    app.state.settings = Settings(training_enabled=True)
    app.include_router(router)
    app.dependency_overrides[require_training_principal] = lambda: principal

    @app.exception_handler(CoireError)
    async def problem(request: Request, error: CoireError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content={"detail": str(error)})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/api/v1/admin/adapters/{adapter_id}"
        denied = await client.patch(
            path,
            headers={"idempotency-key": "publish"},
            json={"expected_version": 1, "visibility": "published"},
        )
        assert denied.status_code == 409
        detail = await client.patch(
            path,
            headers={"idempotency-key": "unpublish"},
            json={"expected_version": 1, "visibility": "admin_only"},
        )
        assert (
            detail.status_code == 200
            and detail.json()["id"] == str(adapter_id)
            and detail.json()["version"] == 2
        )
        retired = await client.post(
            path + "/retire", headers={"idempotency-key": "retire"}, json={"expected_version": 2}
        )
        assert (
            retired.status_code == 200
            and retired.json()["state"] == "retired"
            and retired.json()["version"] == 3
        )
        compatibility = await client.request(
            "DELETE", path, headers={"idempotency-key": "retire"}, json={"expected_version": 2}
        )
        assert compatibility.status_code == 200 and compatibility.json() == {
            "adapter_id": str(adapter_id),
            "state": "retired",
            "version": 3,
        }
        replay = await client.patch(
            path,
            headers={"idempotency-key": "unpublish"},
            json={"expected_version": 1, "visibility": "admin_only"},
        )
        assert replay.json() == detail.json()
    async with database() as session:
        row = await session.get(TrainingAdapterRow, adapter_id)
        assert row is not None and row.state == "retired" and row.version == 3 and not row.verified


async def test_simulated_checkpoint_mirror_commits_and_keeps_grant_secrets_out_of_database(
    database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_api import db
    from coire_api.db import TrainingArtifactCopyRow, TrainingCommandRow
    from coire_api.nodes_client import NodeError, NodeErrorKind
    from coire_api.training_executor import TrainingNodeClient, mirror_checkpoint
    from coire_core.models.training_node import (
        TrainingArtifactGrantIssued,
        TrainingArtifactGrantRequest,
        TrainingArtifactImportRequest,
        TrainingArtifactImportStatus,
        TrainingArtifactVerificationReceipt,
        TrainingArtifactVerifyRequest,
    )

    item = manifest()
    async with database.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        node = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-a"))
        assert job is not None and node is not None
        job.authorization_snapshot = Principal(
            kind=PrincipalKind.USER, user_id=job.owner_user_id, role=UserRole.ADMIN
        ).model_dump(mode="json")
        hold = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node.id,
            holder_type=ReservationHolder.TRAINING,
            holder_id=ATTEMPT,
            bytes=100,
            pinned=True,
            state=MemoryReservationState.HELD,
        )
        session.add(hold)
        await session.flush()
        session.add(
            TrainingParticipantRow(
                attempt_id=ATTEMPT,
                node_id=node.id,
                rank=0,
                reservation_id=hold.id,
                command_id=uuid.uuid4(),
                request_sha256=DIGEST,
                spawn_nonce=uuid.uuid4(),
            )
        )
        await stage_checkpoint(session, item)
    monkeypatch.setattr(db, "_sessionmaker", database)
    secret = "ephemeral-test-grant-secret-123456789"

    class SimulatedClient(TrainingNodeClient):
        def __init__(self) -> None:
            pass

        async def verify_training_artifact(
            self, node: str, artifact_id: uuid.UUID, request: TrainingArtifactVerifyRequest
        ) -> TrainingArtifactVerificationReceipt:
            return TrainingArtifactVerificationReceipt.model_validate(
                {
                    "command_id": request.command_id,
                    "artifact_id": artifact_id,
                    "manifest_sha256": item.canonical_sha256(),
                    "node": node,
                    "verified_bytes": item.total_bytes,
                }
            )

        async def training_artifact_import_status(
            self, node: str, import_id: uuid.UUID
        ) -> TrainingArtifactImportStatus:
            raise NodeError(NodeErrorKind.NOT_FOUND, node)

        async def grant_training_artifact(
            self, request: TrainingArtifactGrantRequest
        ) -> TrainingArtifactGrantIssued:
            return TrainingArtifactGrantIssued(
                grant_id=uuid.uuid4(), secret=secret, expires_at=request.expires_at
            )

        async def import_training_artifact(
            self, request: TrainingArtifactImportRequest
        ) -> TrainingArtifactImportStatus:
            return TrainingArtifactImportStatus(
                import_id=request.command_id,
                artifact_id=item.artifact_id,
                manifest_sha256=item.canonical_sha256(),
                state="verified",
                transferred_bytes=item.total_bytes,
                verified_manifest=item,
            )

    client = SimulatedClient()
    assert await mirror_checkpoint(item.artifact_id, client)
    assert await mirror_checkpoint(item.artifact_id, client)
    async with database() as session:
        checkpoint = await session.get(TrainingCheckpointRow, item.artifact_id)
        assert checkpoint is not None and checkpoint.state == "committed"
        copies = list((await session.scalars(select(TrainingArtifactCopyRow))).all())
        assert len(copies) == 2 and all(copy.state == "verified" for copy in copies)
        commands = list((await session.scalars(select(TrainingCommandRow))).all())
        assert (
            len(
                [
                    command
                    for command in commands
                    if command.operation == "node.training.checkpoint-commit"
                ]
            )
            == 1
        )
        assert all(
            secret not in str(command.payload) and secret not in str(command.receipt)
            for command in commands
        )
