"""A full mirrored boundary and evaluation pause/pin are one replayable transaction."""

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from evaluation_fixtures import FIXTURE
from evaluation_training_fixtures import seed_evaluated_training
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import (
    Base,
    EvaluationCheckpointPinRow,
    MemoryReservationRow,
    NodeRow,
    TrainingArtifactCopyRow,
    TrainingAttemptRow,
    TrainingCheckpointRow,
    TrainingEvaluationTriggerRow,
    TrainingJobRow,
    TrainingParticipantRow,
)
from coire_core.models.training_node import TrainingArtifactManifest
from coire_core.settings import Settings

pytestmark = pytest.mark.integration
CHECKPOINT = json.loads(
    (
        Path(__file__).resolve().parents[4]
        / "tests/fixtures/evaluations/legacy_training/checkpoint.json"
    ).read_bytes()
)


async def boundary(
    session: AsyncSession, *, world_size: int = 1
) -> tuple[TrainingJobRow, TrainingCheckpointRow]:
    job_id, _ = await seed_evaluated_training(session, state="running")
    job = await session.get(TrainingJobRow, job_id, with_for_update=True)
    assert job is not None
    checkpoint = await session.get(TrainingCheckpointRow, job.latest_checkpoint_id)
    assert checkpoint is not None
    original = copy.deepcopy(CHECKPOINT)
    assert job.resolved_spec is not None
    original.update(
        artifact_id=str(checkpoint.id),
        job_id=job.id,
        attempt_id=checkpoint.attempt_id,
        fence=job.fence,
        update=8,
        resolved_spec_sha256=job.resolved_sha256,
        runtime_sha256=job.resolved_spec["runtime_sha256"],
    )
    original["world_size"] = world_size
    if world_size == 2:
        original["files"] += [
            {**item, "id": "rank1-" + item["id"], "name": "rank1_" + item["name"]}
            for item in list(original["files"])
        ]
        rank = copy.deepcopy(original["ranks"][0])
        rank["rank"] = 1
        for field in ("adapter_file_id", "optimizer_file_id", "state_file_id"):
            rank[field] = "rank1-" + rank[field]
        original["ranks"].append(rank)
        original["total_bytes"] *= 2
    manifest = TrainingArtifactManifest.model_validate(original)
    checkpoint.completed_update = 8
    checkpoint.manifest = manifest.model_dump(mode="json")
    checkpoint.manifest_sha256 = manifest.canonical_sha256()
    checkpoint.total_bytes = manifest.total_bytes
    job.completed_update = 8
    job.adapter_id = None
    attempt = await session.get(TrainingAttemptRow, checkpoint.attempt_id)
    assert attempt is not None
    attempt.world_size = world_size
    attempt.state = "running"
    attempt.stopped_at = None
    attempt.lease_expires_at = datetime.now(UTC) + timedelta(seconds=20)
    from coire_core.models.node import NodeRole, Reachability

    node = NodeRow(
        id=uuid.uuid4(),
        name="coire-edge-b",
        role=NodeRole.STUDIO,
        reachability=Reachability.HEALTHY,
        memory_total_bytes=128 * 1024**3,
        disk_total_bytes=1024**4,
        gpu_cores=60,
        agent_version="fixture",
    )
    session.add(node)
    await session.flush()
    nodes = (await session.scalars(select(NodeRow))).all()
    for peer in nodes:
        session.add(
            TrainingArtifactCopyRow(
                artifact_id=checkpoint.id,
                checkpoint_id=checkpoint.id,
                node_id=peer.id,
                manifest_sha256=checkpoint.manifest_sha256,
                storage_key=str(checkpoint.id),
                total_bytes=checkpoint.total_bytes,
                state="verified",
                verified_at=datetime.now(UTC),
            )
        )
    from coire_core.models.placement import MemoryReservationState, ReservationHolder

    for rank, peer in enumerate(sorted(nodes, key=lambda item: item.name)[:world_size]):
        hold = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=peer.id,
            holder_type=ReservationHolder.TRAINING,
            holder_id=attempt.id,
            bytes=1024,
            state=MemoryReservationState.HELD,
        )
        session.add(hold)
        await session.flush()
        session.add(
            TrainingParticipantRow(
                attempt_id=attempt.id,
                node_id=peer.id,
                rank=rank,
                reservation_id=hold.id,
                command_id=uuid.uuid4(),
                request_sha256="a" * 64,
                spawn_nonce=uuid.uuid4(),
                pid=100 + rank,
                process_create_time=datetime.now(UTC).timestamp(),
            )
        )
    await session.commit()
    return job, checkpoint


@pytest.mark.parametrize(
    "condition",
    ["owned", "borrowed", "operator_override", "fence", "expired", "target", "group", "owner"],
)
async def test_checkpoint_candidate_placement_requires_live_exact_ownership(
    training_postgres_url: str, condition: str
) -> None:
    from coire_api.auth import Principal
    from coire_api.db import (
        EvaluationAttemptRow,
        EvaluationGroupRow,
        EvaluationRunRow,
        ModelInstanceRow,
        TrainingAdapterRow,
        UserRow,
    )
    from coire_api.evaluation.training import ensure_checkpoint_trigger
    from coire_api.gateway.targets import ModelNotFoundError, resolve_target
    from coire_api.instance.service import resolve_instance_target
    from coire_core.models.evaluation import (
        EvaluationSuite,
        EvaluationTarget,
        EvaluationWorkload,
        canonical_digest,
    )
    from coire_core.models.instance import InstanceState
    from coire_core.models.training_node import TrainingStopReceipt
    from coire_scheduler.training_recovery import record_stop_proof

    database = create_async_engine(training_postgres_url)
    try:
        async with database.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(database, expire_on_commit=False) as session:
            job, checkpoint = await boundary(session)
            trigger = await ensure_checkpoint_trigger(session, job, checkpoint, settings=Settings())
            assert trigger is not None
            participant = (await session.scalars(select(TrainingParticipantRow))).one()
            node = await session.get(NodeRow, participant.node_id)
            assert node is not None
            await record_stop_proof(
                session,
                participant.attempt_id,
                TrainingStopReceipt(
                    attempt_id=participant.attempt_id,
                    fence=checkpoint.fence,
                    node=node.name,
                    pid=participant.pid,
                    process_create_time=participant.process_create_time,
                    stopped=True,
                    observed_at=datetime.now(UTC),
                ),
            )
            parent = (await session.scalars(select(EvaluationRunRow))).one()
            group = await session.get(EvaluationGroupRow, parent.group_id)
            adapter = (await session.scalars(select(TrainingAdapterRow))).one()
            assert group is not None
            trigger.phase, trigger.group_id, trigger.pause_version = (
                "evaluating",
                group.id,
                job.version,
            )
            group.origin, group.job_id, group.checkpoint_id = (
                "training_checkpoint",
                job.id,
                checkpoint.id,
            )
            adapter.purpose, adapter.evaluation_trigger_id = "evaluation", trigger.id
            base = EvaluationTarget.model_validate(parent.subjects[0])
            target = base.model_copy(
                update={
                    "public_selector": adapter.selector,
                    "target": base.target.model_copy(
                        update={
                            "adapter_id": adapter.id,
                            "adapter_manifest_sha256": adapter.manifest_sha256,
                        }
                    ),
                }
            )
            now = datetime.now(UTC)
            parent.state, parent.started_at, parent.started_deadline_at = (
                "reserving",
                now,
                now + timedelta(minutes=5),
            )
            parent.subjects = [base.model_dump(mode="json"), target.model_dump(mode="json")]
            parent.data_snapshot = {"job_id": job.id, "checkpoint_id": str(checkpoint.id)}
            work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes()).model_copy(
                update={
                    "evaluation_id": parent.id,
                    "attempt_id": uuid.uuid4(),
                    "run_id": uuid.uuid4(),
                    "phase": "candidate",
                    "subject_index": 1,
                    "subject_count": 2,
                    "target": target,
                    "suite": EvaluationSuite.model_validate(parent.suite_snapshot),
                    "deadline": parent.started_deadline_at,
                }
            )
            instance = ModelInstanceRow(
                id=uuid.uuid4(),
                model_id=adapter.model_id,
                variant_id=adapter.base_variant_id,
                adapter_id=adapter.id,
                policy=f"single:{node.name}",
                state=InstanceState.REQUESTED,
            )
            session.add(instance)
            await session.flush()
            attempt = EvaluationAttemptRow(
                id=work.attempt_id,
                run_id=parent.id,
                phase="candidate",
                ordinal=2,
                fence=parent.fence,
                target_sha256=canonical_digest(target),
                workload=work.model_dump(mode="json"),
                deadline_at=work.deadline,
                state="reserving",
                node_id=node.id,
                instance_id=instance.id,
                owns_instance=condition != "borrowed",
            )
            session.add(attempt)
            if condition == "operator_override":
                job.pause_origin = "admin"
            elif condition == "fence":
                attempt.fence += 1
            elif condition == "expired":
                attempt.workload = {
                    **attempt.workload,
                    "deadline": (now - timedelta(seconds=1)).isoformat(),
                }
            elif condition == "target":
                changed = target.model_copy(
                    update={
                        "target": target.target.model_copy(
                            update={"adapter_manifest_sha256": "f" * 64}
                        )
                    }
                )
                attempt.workload = {**attempt.workload, "target": changed.model_dump(mode="json")}
            elif condition == "group":
                trigger.group_id = None
            elif condition == "owner":
                owner = await session.get(UserRow, parent.owner_user_id)
                assert owner is not None
                owner.active = False
            await session.commit()
            with pytest.raises(ModelNotFoundError):
                await resolve_target(
                    session, adapter.selector, Principal.model_validate(job.authorization_snapshot)
                )
            if condition == "owned":
                selected = await resolve_instance_target(session, instance)
                assert selected.adapter is adapter and selected.identity == target.target
            else:
                with pytest.raises(ModelNotFoundError):
                    await resolve_instance_target(session, instance)
    finally:
        await database.dispose()


async def test_boundary_obligation_pause_and_pin_replay_under_disabled_admission(
    training_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_api.evaluation.training import ensure_checkpoint_trigger
    from coire_core.errors import TrainingConflict

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job, checkpoint = await boundary(session)
            first = await ensure_checkpoint_trigger(
                session, job, checkpoint, settings=Settings(evaluations_enabled=False)
            )
            assert first is not None
            assert (
                job.state == "pausing"
                and job.pause_origin == "evaluation"
                and job.evaluation_pause_trigger_id == first.id
            )
            assert first.pause_version == job.version and len(first.schedules) == 1
            await session.commit()
            repeated = await ensure_checkpoint_trigger(
                session, job, checkpoint, settings=Settings()
            )
            assert repeated is not None and repeated.id == first.id
            assert (
                await session.scalar(select(func.count()).select_from(EvaluationCheckpointPinRow))
                == 1
            )
            assert (
                await session.scalar(select(func.count()).select_from(TrainingEvaluationTriggerRow))
                == 1
            )
            # Repeated metadata processing must preserve newer operator intent.
            job.pause_origin = "admin"
            job.safe_reason = "admin_pause"
            job.version += 1
            await session.commit()
            overridden = await ensure_checkpoint_trigger(
                session, job, checkpoint, settings=Settings()
            )
            assert overridden is not None and overridden.id == first.id
            assert job.pause_origin == "admin"
            # A second boundary cannot accumulate another pin while the first is unresolved.
            previous = TrainingArtifactManifest.model_validate(checkpoint.manifest)
            changed = previous.model_copy(
                update={
                    "update": 16,
                    "ranks": [rank.model_copy(update={"update": 16}) for rank in previous.ranks],
                }
            )
            checkpoint.completed_update = 16
            checkpoint.manifest = changed.model_dump(mode="json")
            checkpoint.manifest_sha256 = changed.canonical_sha256()
            with pytest.raises(TrainingConflict, match="earlier evaluation boundary"):
                await ensure_checkpoint_trigger(session, job, checkpoint, settings=Settings())
            assert (
                await session.scalar(select(func.count()).select_from(EvaluationCheckpointPinRow))
                == 1
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize("operator_override", [False, True])
async def test_expired_disabled_checkpoint_records_failure_and_settles_pause_owner(
    training_postgres_url: str, operator_override: bool
) -> None:
    from coire_api.db import EvaluationGroupRow, EvaluationRunRow
    from coire_api.evaluation.checkpoints import finish_checkpoint_cleanup
    from coire_api.evaluation.training import ensure_checkpoint_trigger, reconcile_trigger
    from coire_core.models.training_node import TrainingStopReceipt
    from coire_scheduler.training_recovery import record_stop_proof

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job, checkpoint = await boundary(session)
            settings = Settings(evaluations_enabled=False, training_enabled=True)
            trigger = await ensure_checkpoint_trigger(session, job, checkpoint, settings=settings)
            assert trigger is not None
            participant = (await session.scalars(select(TrainingParticipantRow))).one()
            node = await session.get(NodeRow, participant.node_id)
            assert node is not None
            await record_stop_proof(
                session,
                participant.attempt_id,
                TrainingStopReceipt(
                    attempt_id=participant.attempt_id,
                    fence=checkpoint.fence,
                    node=node.name,
                    pid=participant.pid,
                    process_create_time=participant.process_create_time,
                    stopped=True,
                    observed_at=datetime.now(UTC),
                ),
            )
            if operator_override:
                job.pause_origin = "admin"
                job.version += 1
            trigger.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
            assert not await reconcile_trigger(
                session, trigger.id, settings=settings, boundary="checkpoint"
            )
            run = (
                await session.scalars(
                    select(EvaluationRunRow).where(EvaluationRunRow.group_id == trigger.group_id)
                )
            ).one()
            group = await session.get(EvaluationGroupRow, run.group_id)
            assert group is not None and group.origin == "training_checkpoint"
            assert run.state == "failed" and run.safe_failure_code == "admission_disabled"
            assert run.cleanup_state == "complete" and run.evidence_reserved_bytes == 0
            assert trigger.phase == "cleaning_adapter" and job.state == "paused"
            await finish_checkpoint_cleanup(session, trigger.id, settings=settings)
            assert trigger.phase == ("complete" if operator_override else "resume_pending")
            assert job.state == "paused"
            if operator_override:
                assert job.pause_origin == "admin" and job.evaluation_pause_trigger_id is None
            else:
                assert (
                    job.pause_origin == "evaluation"
                    and job.evaluation_pause_trigger_id == trigger.id
                )
            await session.commit()
            original = trigger.group_id
            assert (
                await reconcile_trigger(
                    session, trigger.id, settings=settings, boundary="checkpoint"
                )
                is operator_override
            )
            assert trigger.group_id == original
            if not operator_override:
                # A later operator command wins even after erasure has finished.
                job.pause_origin, job.safe_reason = "admin", "admin_pause"
                job.version += 1
                await session.commit()
                assert await finish_checkpoint_cleanup(session, trigger.id, settings=settings)
                assert trigger.resume_disposition == "operator_override"
                assert job.evaluation_pause_trigger_id is None and job.state == "paused"
    finally:
        await engine.dispose()


async def test_operator_pause_overrides_evaluation_ownership_without_resuming(
    training_postgres_url: str,
) -> None:
    from coire_api.auth import Principal
    from coire_api.evaluation.training import ensure_checkpoint_trigger
    from coire_api.training.service import control_training
    from coire_core.models.training import TrainingControlRequest

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job, checkpoint = await boundary(session)
            trigger = await ensure_checkpoint_trigger(session, job, checkpoint, settings=Settings())
            assert trigger is not None
            await session.commit()
            prior = job.version
            receipt = await control_training(
                session,
                Principal.model_validate(job.authorization_snapshot),
                job.id,
                "pause",
                TrainingControlRequest(expected_version=prior),
                "override-pause",
            )
            assert receipt.version == prior + 1 and job.pause_origin == "admin"
            assert job.state == "pausing" and trigger.pause_version == prior
    finally:
        await engine.dispose()


async def test_durable_checkpoint_acknowledgment_carries_atomic_pause_and_replays(
    training_postgres_url: str,
) -> None:
    from coire_core.models.training_node import (
        CheckpointCommitAcknowledgementV2,
        parse_checkpoint_acknowledgement,
    )
    from coire_scheduler.training import enqueue_checkpoint_acknowledgements

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job, checkpoint = await boundary(session)
            first = await enqueue_checkpoint_acknowledgements(session, checkpoint.id)
            await session.commit()
            trigger = (await session.scalars(select(TrainingEvaluationTriggerRow))).one()
            assert job.state == "pausing" and trigger.pause_version == job.version
            assert len(first) == 1
            command = first[0]
            ack = parse_checkpoint_acknowledgement(command.payload)
            assert isinstance(ack, CheckpointCommitAcknowledgementV2)
            assert (
                ack.evaluation_pause is not None and ack.evaluation_pause.trigger_id == trigger.id
            )
            assert (
                ack.job_version == job.version
                and ack.committed_update == checkpoint.completed_update
            )
            original = command.payload.copy()
            job.pause_origin = "admin"
            job.version += 1
            await session.commit()
            assert [
                row.id for row in await enqueue_checkpoint_acknowledgements(session, checkpoint.id)
            ] == [row.id for row in first]
            await session.refresh(command)
            assert command.payload == original and job.pause_origin == "admin"
            from coire_api.evaluation.links import for_job

            point = next(
                link for link in await for_job(session, job.id) if link.trigger_id == trigger.id
            )
            assert point.attempt_id == checkpoint.attempt_id and point.fence == checkpoint.fence
            assert point.pause_owner == "admin" and point.trigger_phase == "pending_pause"
    finally:
        await engine.dispose()


async def test_two_rank_stop_proofs_precede_evaluation_and_release(
    training_postgres_url: str,
) -> None:
    from coire_api.evaluation.training import ensure_checkpoint_trigger, require_checkpoint_pause
    from coire_core.errors import TrainingConflict
    from coire_core.models.placement import MemoryReservationState
    from coire_core.models.training_node import TrainingStopReceipt
    from coire_scheduler.training_recovery import record_stop_proof

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job, checkpoint = await boundary(session, world_size=2)
            trigger = await ensure_checkpoint_trigger(session, job, checkpoint, settings=Settings())
            assert trigger is not None
            await session.commit()
            participants = (
                await session.scalars(
                    select(TrainingParticipantRow).order_by(TrainingParticipantRow.rank)
                )
            ).all()
            for index, participant in enumerate(participants):
                node = await session.get(NodeRow, participant.node_id)
                assert node is not None
                proof = TrainingStopReceipt(
                    attempt_id=participant.attempt_id,
                    fence=checkpoint.fence,
                    node=node.name,
                    pid=participant.pid,
                    process_create_time=participant.process_create_time,
                    stopped=True,
                    observed_at=datetime.now(UTC),
                )
                assert await record_stop_proof(session, participant.attempt_id, proof) is (
                    index == 1
                )
                await session.commit()
                if index == 0:
                    assert job.state == "pausing"
                    for peer in participants:
                        hold = await session.get(
                            MemoryReservationRow, peer.reservation_id, populate_existing=True
                        )
                        assert hold is not None and hold.state == MemoryReservationState.HELD
                    with pytest.raises(TrainingConflict):
                        await require_checkpoint_pause(session, job, trigger.id)
                else:
                    assert job.state == "paused" and job.pause_origin == "evaluation"
                    assert (
                        await require_checkpoint_pause(session, job, trigger.id)
                    ).pause_version == job.version
            with pytest.raises(TrainingConflict):
                await record_stop_proof(
                    session, proof.attempt_id, proof.model_copy(update={"fence": 2})
                )
    finally:
        await engine.dispose()


@pytest.mark.parametrize("wrong_receipt", [False, True])
@pytest.mark.parametrize(
    "import_condition", ["present", "absent", "unreachable", "foreign", "uncertain"]
)
async def test_private_adapter_erasure_is_owned_and_proof_bound(
    training_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    wrong_receipt: bool,
    import_condition: str,
) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    import coire_scheduler.evaluation_checkpoints as module
    from coire_api.db import TrainingAdapterRow, TrainingStorageReservationRow
    from coire_api.evaluation.training import ensure_checkpoint_trigger
    from coire_api.nodes_client import NodeError, NodeErrorKind
    from coire_core.errors import TrainingConflict
    from coire_core.models.training_node import (
        TrainingArtifactDeletionReceipt,
        TrainingArtifactImportStatus,
    )

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job, checkpoint = await boundary(session)
            trigger = await ensure_checkpoint_trigger(session, job, checkpoint, settings=Settings())
            assert trigger is not None
            trigger.phase = "cleaning_adapter"
            adapter_id = uuid.uuid5(trigger.id, "checkpoint-adapter")
            checkpoint_manifest = TrainingArtifactManifest.model_validate(checkpoint.manifest)
            assert job.resolved_spec is not None
            from coire_core.models.training import parse_resolved_training_spec

            resolved = parse_resolved_training_spec(job.resolved_spec)
            artifact = TrainingArtifactManifest(
                artifact_id=adapter_id,
                kind="adapter",
                job_id=job.id,
                attempt_id=checkpoint.attempt_id,
                fence=checkpoint.fence,
                update=checkpoint.completed_update,
                runtime_sha256=checkpoint_manifest.runtime_sha256,
                resolved_spec_sha256=job.resolved_sha256,
                files=TrainingArtifactManifest.model_validate(checkpoint.manifest).files[:2],
                total_bytes=sum(
                    item.bytes
                    for item in TrainingArtifactManifest.model_validate(checkpoint.manifest).files[
                        :2
                    ]
                ),
            )
            adapter = TrainingAdapterRow(
                id=adapter_id,
                model_id=job.model_id,
                base_variant_id=job.base_variant_id,
                source_job_id=job.id,
                source_checkpoint_id=checkpoint.id,
                slug=f"eval-{trigger.id.hex}",
                selector=f"{job.model_id}@eval-{trigger.id.hex}",
                purpose="evaluation",
                evaluation_trigger_id=trigger.id,
                base_manifest_sha256=resolved.base_manifest_sha256,
                manifest_sha256=artifact.canonical_sha256(),
                resolved_spec_sha256=job.resolved_sha256,
                parameterization="lora",
                state="ready",
                metadata_record={"manifest": artifact.model_dump(mode="json")},
            )
            hold = TrainingStorageReservationRow(
                id=uuid.uuid4(),
                owner_user_id=job.owner_user_id,
                subject_id=str(adapter_id),
                bytes=100,
                state="held",
            )
            session.add_all([adapter, hold])
            await session.commit()

        @asynccontextmanager
        async def scope() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as owned:
                yield owned
                await owned.commit()

        monkeypatch.setattr(module, "session_scope", scope)
        client = AsyncMock()
        calls: list[tuple[str, uuid.UUID]] = []

        async def delete(
            node: str, identity: uuid.UUID, request: object
        ) -> TrainingArtifactDeletionReceipt:
            from coire_core.models.training_node import TrainingArtifactDeleteRequest

            assert isinstance(request, TrainingArtifactDeleteRequest)
            calls.append((node, identity))
            return TrainingArtifactDeletionReceipt(
                command_id=request.command_id,
                artifact_id=uuid.uuid4() if wrong_receipt else identity,
                purged=True,
            )

        client.delete_training_artifact.side_effect = delete

        async def status(node: str, import_id: uuid.UUID) -> TrainingArtifactImportStatus:
            if import_condition == "absent":
                raise NodeError(NodeErrorKind.NOT_FOUND, node, status=404)
            if import_condition == "unreachable":
                raise NodeError(NodeErrorKind.UNREACHABLE, node)
            return TrainingArtifactImportStatus(
                import_id=import_id,
                artifact_id=uuid.uuid4() if import_condition == "foreign" else adapter_id,
                manifest_sha256=artifact.canonical_sha256(),
                state="transferring",
                transferred_bytes=0,
            )

        async def cancel(node: str, import_id: uuid.UUID) -> TrainingArtifactImportStatus:
            return TrainingArtifactImportStatus(
                import_id=import_id,
                artifact_id=adapter_id,
                manifest_sha256=artifact.canonical_sha256(),
                state="transferring" if import_condition == "uncertain" else "cancelled",
                transferred_bytes=0,
            )

        client.training_artifact_import_status.side_effect = status
        client.cancel_training_artifact_import.side_effect = cancel
        blocked_import = import_condition not in {"present", "absent"}
        if import_condition == "unreachable":
            with pytest.raises(NodeError):
                await module.erase(trigger.id, client)
        elif blocked_import:
            with pytest.raises(TrainingConflict):
                await module.erase(trigger.id, client)
        elif wrong_receipt:
            with pytest.raises(TrainingConflict, match="unproved"):
                await module.erase(trigger.id, client)
        else:
            assert await module.erase(trigger.id, client)
            assert await module.erase(trigger.id, client)
        assert all(identity == adapter_id for _, identity in calls)
        assert len(calls) == (0 if blocked_import else 1 if wrong_receipt else 2)
        async with AsyncSession(engine) as session:
            retained = await session.get(TrainingStorageReservationRow, hold.id)
            current = await session.get(TrainingAdapterRow, adapter_id)
            assert retained is not None and current is not None
            assert retained.state == ("held" if wrong_receipt or blocked_import else "released")
            assert current.state == ("ready" if wrong_receipt or blocked_import else "retired")
            pins = (await session.scalars(select(EvaluationCheckpointPinRow))).all()
            assert pins and all(pin.released_at is None for pin in pins)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("interruption", [None, "admin", "profile_expired", "admission_disabled"])
async def test_checkpoint_resume_rechecks_newer_intent_and_fresh_profile(
    training_postgres_url: str, monkeypatch: pytest.MonkeyPatch, interruption: str | None
) -> None:
    from unittest.mock import AsyncMock

    from sqlalchemy.ext.asyncio import async_sessionmaker
    from training_measurement_fixtures import persist_measured_profile

    from coire_api.db import TrainingCommandRow, TrainingProfileRow
    from coire_api.evaluation.training import ensure_checkpoint_trigger
    from coire_core.models.training import ResolvedTrainingSpec, TrainingProfile
    from coire_scheduler.training_controller import TrainingController

    engine = create_async_engine(training_postgres_url)
    settings = Settings(training_enabled=True)
    monkeypatch.setattr("coire_core.settings.get_settings", lambda: settings)
    # These two unrelated input/telemetry gates are tested separately. This
    # transaction test exercises real profile rows and intent changes during I/O.
    monkeypatch.setattr("coire_scheduler.training_controller.recheck_training_inputs", AsyncMock())
    monkeypatch.setattr(
        "coire_scheduler.training_guard.recheck_resource_evidence", AsyncMock(return_value=None)
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            job, checkpoint = await boundary(session)
            trigger = await ensure_checkpoint_trigger(session, job, checkpoint, settings=settings)
            assert trigger is not None
            trigger.phase = "resume_pending"
            job.state = "paused"
            attempt = await session.get(TrainingAttemptRow, checkpoint.attempt_id)
            assert attempt is not None
            attempt.state = "stopped"
            data = json.loads(json.dumps(job.resolved_spec))
            assert data is not None
            data.pop("evaluations")
            data.pop("evaluation_base")
            data["spec"]["schema_version"] = 1
            data["spec"]["eval"].pop("suites")
            resolved = ResolvedTrainingSpec.model_validate(data)
            nodes = list((await session.scalars(select(NodeRow))).all())
            await persist_measured_profile(
                session,
                job.owner_user_id,
                resolved,
                nodes[: 2 if resolved.spec.placement.mode == "data_parallel" else 1],
            )
            profile_row = (await session.scalars(select(TrainingProfileRow))).one()
            profile = TrainingProfile.model_validate(profile_row.profile)
            job_id, trigger_id = job.id, trigger.id
            await session.commit()

        class ResumeTransport:
            async def resume_profile(self, requested_job: str) -> TrainingProfile:
                assert requested_job == job_id
                async with factory.begin() as session:
                    current = await session.get(TrainingJobRow, job_id, with_for_update=True)
                    assert current is not None
                    if interruption == "admin":
                        current.pause_origin = "admin"
                        current.version += 1
                    elif interruption == "profile_expired":
                        row = await session.get(TrainingProfileRow, profile.id)
                        assert row is not None
                        row.valid_until = datetime.now(UTC) - timedelta(seconds=1)
                    elif interruption == "admission_disabled":
                        settings.training_enabled = False
                return profile

            async def guard_reason(self, attempt_id: str) -> None:
                return None

        controller = TrainingController(ResumeTransport(), sessions=factory.begin)  # type: ignore[arg-type]
        await controller._resume_automatic(job_id, origin="evaluation")
        await controller._resume_automatic(job_id, origin="evaluation")
        async with factory() as session:
            recovered_job = await session.get(TrainingJobRow, job_id)
            trigger = await session.get(TrainingEvaluationTriggerRow, trigger_id)
            assert recovered_job is not None and trigger is not None
            commands = list(
                (
                    await session.scalars(
                        select(TrainingCommandRow).where(
                            TrainingCommandRow.operation == "training.evaluation.resume"
                        )
                    )
                ).all()
            )
            if interruption is None:
                assert recovered_job.state == "queued" and recovered_job.pause_origin is None
                assert recovered_job.evaluation_pause_trigger_id is None
                assert trigger.phase == "complete" and trigger.resume_disposition == "resumed"
                assert len(commands) == 1 and commands[0].id == uuid.uuid5(
                    trigger_id, "evaluation-resume"
                )
                assert commands[0].payload["checkpoint_id"] == str(checkpoint.id)
                assert commands[0].payload["profile_sha256"] == profile.report_sha256
            else:
                assert recovered_job.state == "paused" and not commands
                assert trigger.phase == "resume_pending"
                assert recovered_job.pause_origin == (
                    "admin" if interruption == "admin" else "evaluation"
                )
    finally:
        await engine.dispose()


async def test_private_checkpoint_adapter_is_hidden_and_cannot_be_curated(
    training_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from coire_api.auth import Principal
    from coire_api.db import TrainingAdapterRow
    from coire_api.evaluation.training import ensure_checkpoint_trigger
    from coire_api.routes import admin_adapters
    from coire_api.training.authorization import require_training_principal
    from coire_core.errors import CoireError

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job, checkpoint = await boundary(session)
            trigger = await ensure_checkpoint_trigger(session, job, checkpoint, settings=Settings())
            assert trigger is not None
            original = (await session.scalars(select(TrainingAdapterRow))).one()
            # This fixture is not published or used for generation. Create the
            # immutable private purpose at insertion rather than mutate a serving row.
            values = {
                column.name: getattr(original, column.name)
                for column in TrainingAdapterRow.__table__.columns
            }
            values.update(
                id=uuid.uuid5(trigger.id, "checkpoint-adapter"),
                purpose="evaluation",
                evaluation_trigger_id=trigger.id,
                verified=False,
                visibility="admin_only",
                slug=f"eval-{trigger.id.hex}",
                selector=f"{job.model_id}@eval-{trigger.id.hex}",
                metadata={},
            )
            values.pop("metadata")
            values["metadata_record"] = {}
            private = TrainingAdapterRow(**values)
            session.add(private)
            # The synthetic serving seed is not a complete console fixture.
            await session.delete(original)
            await session.commit()
            principal = Principal.model_validate(job.authorization_snapshot)
            identity = private.id

        @asynccontextmanager
        async def scope() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session
                await session.commit()

        monkeypatch.setattr(admin_adapters, "session_scope", scope)
        app = FastAPI()
        app.state.settings = Settings(training_enabled=True)
        app.include_router(admin_adapters.router)
        app.dependency_overrides[require_training_principal] = lambda: principal

        @app.exception_handler(CoireError)
        async def problem(request: Request, error: CoireError) -> JSONResponse:
            return JSONResponse(status_code=error.status, content={"code": error.code})

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            page = await client.get("/api/v1/admin/adapters")
            assert page.status_code == 200 and page.json()["items"] == []
            path = f"/api/v1/admin/adapters/{identity}"
            assert (await client.get(path)).status_code == 404
            response = await client.patch(
                path,
                headers={"Idempotency-Key": "publish-private"},
                json={"expected_version": 1, "visibility": "published"},
            )
            assert response.status_code in {404, 409}, response.text
        async with scope() as session:
            persisted = await session.get(TrainingAdapterRow, identity)
            assert persisted is not None and persisted.purpose == "evaluation"
            assert not persisted.verified and persisted.visibility == "admin_only"
    finally:
        await engine.dispose()


async def test_lost_checkpoint_rank_keeps_training_holds_and_retention_pin(
    training_postgres_url: str,
) -> None:
    from coire_api.evaluation.cleanup import release_trigger_pins
    from coire_api.evaluation.training import ensure_checkpoint_trigger, require_checkpoint_pause
    from coire_core.errors import TrainingConflict
    from coire_core.models.node import Reachability
    from coire_core.models.placement import MemoryReservationState
    from coire_core.models.training_node import TrainingStopReceipt
    from coire_scheduler.training_recovery import record_stop_proof

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job, checkpoint = await boundary(session, world_size=2)
            trigger = await ensure_checkpoint_trigger(session, job, checkpoint, settings=Settings())
            assert trigger is not None
            participants = list(
                (
                    await session.scalars(
                        select(TrainingParticipantRow).order_by(TrainingParticipantRow.rank)
                    )
                ).all()
            )
            first, lost = participants
            node = await session.get(NodeRow, first.node_id)
            unreachable = await session.get(NodeRow, lost.node_id)
            assert node is not None and unreachable is not None
            unreachable.reachability = Reachability.UNREACHABLE
            assert not await record_stop_proof(
                session,
                first.attempt_id,
                TrainingStopReceipt(
                    attempt_id=first.attempt_id,
                    fence=checkpoint.fence,
                    node=node.name,
                    pid=first.pid,
                    process_create_time=first.process_create_time,
                    stopped=True,
                    observed_at=datetime.now(UTC),
                ),
            )
            with pytest.raises(TrainingConflict):
                await record_stop_proof(
                    session,
                    lost.attempt_id,
                    TrainingStopReceipt(
                        attempt_id=lost.attempt_id,
                        fence=checkpoint.fence,
                        node=unreachable.name,
                        pid=lost.pid,
                        process_create_time=lost.process_create_time,
                        stopped=False,
                        observed_at=datetime.now(UTC),
                    ),
                )
            await session.commit()
        # Restart does not reinterpret one reachable rank's stop as group death.
        async with AsyncSession(engine, expire_on_commit=False) as session:
            recovered = await session.get(TrainingJobRow, job.id)
            assert recovered is not None and recovered.state == "pausing"
            with pytest.raises(TrainingConflict):
                await require_checkpoint_pause(session, recovered, trigger.id)
            assert await release_trigger_pins(session, trigger.id, now=datetime.now(UTC)) == 0
            assert all(
                hold.state == MemoryReservationState.HELD
                for hold in (await session.scalars(select(MemoryReservationRow))).all()
            )
            pin = (await session.scalars(select(EvaluationCheckpointPinRow))).one()
            assert pin.released_at is None
    finally:
        await engine.dispose()
