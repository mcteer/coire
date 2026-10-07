"""Real Postgres controller replay, cancellation, lease and process-proof tests."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
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
    TrainingCommandRow,
    TrainingJobRow,
    TrainingParticipantRow,
    UserRow,
)
from coire_core.models.auth import UserRole
from coire_core.models.engine import EngineStatus
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.training import ResolvedTrainingSpec, TrainingProfile, TrainingReason
from coire_core.models.training_node import (
    NodeTrainingEventPage,
    NodeTrainingStatus,
    TrainingArtifactManifest,
    TrainingStopReceipt,
)
from coire_scheduler.training_controller import TrainingController
from coire_scheduler.training_recovery import record_stop_proof

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1", reason="requires disposable PostgreSQL"
    ),
]
JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ATTEMPT = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
DIGEST = "a" * 64


@pytest.fixture
async def controller_db(
    training_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(training_postgres_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        owner = UserRow(
            id=uuid.uuid4(),
            email="controller@training.test",
            display_name="test",
            role=UserRole.ADMIN,
            active=True,
        )
        model = ModelRow(
            id=uuid.uuid4(),
            repo_id="synthetic/controller",
            slug="controller",
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
        session.add_all([owner, model])
        await session.flush()
        variant = ModelVariantRow(
            id=uuid.uuid4(),
            model_id=model.id,
            name="test",
            slug="test",
            precision="bf16",
            state="ready",
            source_revision="fixture",
        )
        node = NodeRow(
            id=uuid.uuid4(),
            name="coire-edge-a",
            role="studio",
            memory_total_bytes=10000,
            disk_total_bytes=10000,
            agent_version="fixture",
        )
        session.add_all([variant, node])
        await session.flush()
        principal = Principal(
            kind=PrincipalKind.USER, subject=str(owner.id), user_id=owner.id, role=UserRole.ADMIN
        )
        now = datetime.now(UTC)
        session.add(
            TrainingJobRow(
                id=JOB,
                owner_user_id=owner.id,
                model_id=model.id,
                base_variant_id=variant.id,
                idempotency_key="controller",
                output_slug="output",
                source_yaml="{}",
                source_sha256=DIGEST,
                intent_sha256=DIGEST,
                submitted_spec={},
                resolved_sha256=DIGEST,
                authorization_snapshot=principal.model_dump(mode="json"),
                state="running",
                fence=1,
                queue_deadline_at=now + timedelta(hours=1),
                execution_deadline_at=now + timedelta(hours=1),
            )
        )
        await session.flush()
        session.add(
            TrainingAttemptRow(
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
        await session.flush()
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
                process_create_time=now.timestamp(),
            )
        )
    try:
        yield factory
    finally:
        await engine.dispose()


class SimulatedTransport:
    """No engines: exercise actual DB journal/proofs around uncertain node I/O."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.deliveries: list[uuid.UUID] = []
        self.stop_available = False
        self.liveness = "running"
        self.reason: TrainingReason | None = None
        self.status_calls = 0

    async def dispatch_command(self, command_id: uuid.UUID) -> None:
        async with self.factory.begin() as session:
            row = await session.get(TrainingCommandRow, command_id, with_for_update=True)
            assert row is not None
            if row.state == "succeeded":
                return
            operation = row.operation
            node = row.subject_id
            row.state = "dispatching"
            self.deliveries.append(command_id)
        if operation == "node.training.stop":
            if not self.stop_available:
                raise TimeoutError("simulated partition")
            async with self.factory.begin() as session:
                p = await session.scalar(
                    select(TrainingParticipantRow)
                    .join(NodeRow, NodeRow.id == TrainingParticipantRow.node_id)
                    .where(TrainingParticipantRow.attempt_id == ATTEMPT, NodeRow.name == node)
                )
                assert p is not None
                proof = TrainingStopReceipt(
                    attempt_id=ATTEMPT,
                    fence=1,
                    node=node,
                    pid=p.pid,
                    process_create_time=p.process_create_time,
                    stopped=True,
                    observed_at=datetime.now(UTC),
                )
                await record_stop_proof(session, ATTEMPT, proof)
                row = await session.get(TrainingCommandRow, command_id, with_for_update=True)
                assert row is not None
                row.receipt, row.state = proof.model_dump(mode="json"), "succeeded"
            self.liveness = "stopped"
        elif operation == "node.training.pause":
            # Request acknowledgement is not a death proof or checkpoint.
            async with self.factory.begin() as session:
                row = await session.get(TrainingCommandRow, command_id, with_for_update=True)
                assert row is not None
                row.state = "succeeded"
        else:
            raise AssertionError(f"unexpected node command {operation}")

    async def training_events(
        self, node: str, attempt_id: str, after: int
    ) -> NodeTrainingEventPage:
        return NodeTrainingEventPage(items=[], next_sequence=after)

    async def training_status(self, node: str, attempt_id: str) -> NodeTrainingStatus:
        self.status_calls += 1
        async with self.factory() as session:
            p = await session.scalar(
                select(TrainingParticipantRow)
                .join(NodeRow, NodeRow.id == TrainingParticipantRow.node_id)
                .where(TrainingParticipantRow.attempt_id == attempt_id, NodeRow.name == node)
            )
            attempt = await session.get(TrainingAttemptRow, attempt_id)
            assert p is not None and attempt is not None
            return NodeTrainingStatus.model_validate(
                {
                    "attempt_id": attempt_id,
                    "job_id": JOB,
                    "fence": 1,
                    "node": node,
                    "liveness": self.liveness,
                    "pid": p.pid,
                    "process_create_time": p.process_create_time,
                    "update": 0,
                    "lease_expires_at": attempt.lease_expires_at,
                }
            )

    async def mirror_checkpoint(self, checkpoint_id: uuid.UUID) -> bool:
        raise AssertionError("no staged checkpoint in this fixture")

    async def extract_final_adapter(
        self, checkpoint_id: uuid.UUID, adapter_id: uuid.UUID, command_id: uuid.UUID
    ) -> TrainingArtifactManifest | None:
        raise AssertionError("no completed checkpoint in this fixture")

    async def prepare_adapter(self, adapter_id: uuid.UUID) -> tuple[uuid.UUID, EngineStatus] | None:
        raise AssertionError("no final adapter in this fixture")

    async def guard_reason(self, attempt_id: str) -> TrainingReason | None:
        return self.reason

    async def resume_profile(self, job_id: str) -> TrainingProfile | None:
        return None


async def state(factory: async_sessionmaker[AsyncSession]) -> tuple[str, MemoryReservationState]:
    async with factory() as session:
        job = await session.get(TrainingJobRow, JOB)
        hold = await session.scalar(select(MemoryReservationRow))
        assert job is not None and hold is not None
        return job.state, hold.state


async def test_cancel_partition_restart_replays_same_stop_and_retains_hold(
    controller_db: async_sessionmaker[AsyncSession],
) -> None:
    async with controller_db.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        job.state, job.safe_reason = "cancelling", "cancelled"
    transport = SimulatedTransport(controller_db)
    await TrainingController(transport, sessions=controller_db.begin).run_once()
    assert await state(controller_db) == ("cancelling", MemoryReservationState.HELD)
    async with controller_db() as session:
        commands = (await session.scalars(select(TrainingCommandRow))).all()
        assert len(commands) == 1 and commands[0].state == "dispatching"
        command_id = commands[0].id
    transport.stop_available = True
    await TrainingController(transport, sessions=controller_db.begin).run_once()
    assert await state(controller_db) == ("cancelled", MemoryReservationState.RELEASED)
    assert set(transport.deliveries) == {command_id}


async def test_scheduler_restart_readopts_live_trainer_without_another_start(
    controller_db: async_sessionmaker[AsyncSession],
) -> None:
    transport = SimulatedTransport(controller_db)
    for _ in range(2):
        await TrainingController(transport, sessions=controller_db.begin).run_once()
    assert transport.status_calls == 2
    assert not transport.deliveries
    assert await state(controller_db) == ("running", MemoryReservationState.HELD)


async def test_concurrent_cancel_ticks_commit_one_stop_and_one_release(
    controller_db: async_sessionmaker[AsyncSession],
) -> None:
    async with controller_db.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        job.state, job.safe_reason = "cancelling", "cancelled"
    transport = SimulatedTransport(controller_db)
    transport.stop_available = True
    await asyncio.gather(
        *(TrainingController(transport, sessions=controller_db.begin).tick(JOB) for _ in range(2))
    )
    assert await state(controller_db) == ("cancelled", MemoryReservationState.RELEASED)
    async with controller_db() as session:
        commands = (await session.scalars(select(TrainingCommandRow))).all()
        assert len(commands) == 1 and commands[0].state == "succeeded"
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None and job.next_event_sequence == 2


async def test_execution_timeout_requests_stop_but_does_not_assume_death(
    controller_db: async_sessionmaker[AsyncSession],
) -> None:
    async with controller_db.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        job.execution_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    transport = SimulatedTransport(controller_db)
    await TrainingController(transport, sessions=controller_db.begin).tick(JOB)
    assert await state(controller_db) == ("cancelling", MemoryReservationState.HELD)
    async with controller_db() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None and job.safe_reason == "execution_timeout"


async def test_guard_pause_keeps_hold_until_sixty_second_forced_stop(
    controller_db: async_sessionmaker[AsyncSession],
) -> None:
    transport = SimulatedTransport(controller_db)
    transport.reason = "memory_breach"
    controller = TrainingController(transport, sessions=controller_db.begin)
    await controller.tick(JOB)
    assert await state(controller_db) == ("pausing", MemoryReservationState.HELD)
    async with controller_db.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        assert job.pause_origin == "protective"
        job.updated_at = datetime.now(UTC) - timedelta(seconds=61)
    transport.stop_available = True
    await controller.tick(JOB)
    assert await state(controller_db) == ("paused", MemoryReservationState.RELEASED)


async def test_terminal_jobs_never_dispatch_or_resurrect(
    controller_db: async_sessionmaker[AsyncSession],
) -> None:
    async with controller_db.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        job.state = "cancelled"
    transport = SimulatedTransport(controller_db)
    await TrainingController(transport, sessions=controller_db.begin).run_once()
    assert not transport.deliveries and transport.status_calls == 0


async def test_unknown_node_keeps_hold_and_enters_coordinated_recovery(
    controller_db: async_sessionmaker[AsyncSession],
) -> None:
    class Partitioned(SimulatedTransport):
        async def training_status(self, node: str, attempt_id: str) -> NodeTrainingStatus:
            raise TimeoutError("simulated node partition")

    transport = Partitioned(controller_db)
    await TrainingController(transport, sessions=controller_db.begin).tick(JOB)
    assert await state(controller_db) == ("recovering", MemoryReservationState.HELD)
    async with controller_db() as session:
        attempts = (await session.scalars(select(TrainingAttemptRow))).all()
        assert len(attempts) == 1 and attempts[0].state == "stopping"
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None and job.safe_reason == "node_unreachable"


async def test_expired_lease_is_not_renewed_or_treated_as_death(
    controller_db: async_sessionmaker[AsyncSession],
) -> None:
    async with controller_db.begin() as session:
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        assert attempt is not None
        attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    transport = SimulatedTransport(controller_db)
    await TrainingController(transport, sessions=controller_db.begin).tick(JOB)
    assert await state(controller_db) == ("recovering", MemoryReservationState.HELD)
    async with controller_db() as session:
        operations = (await session.scalars(select(TrainingCommandRow.operation))).all()
        assert operations == ["node.training.stop"]


async def test_queue_deadline_is_terminal_without_node_side_effects(
    controller_db: async_sessionmaker[AsyncSession],
) -> None:
    async with controller_db.begin() as session:
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        job = await session.get(TrainingJobRow, JOB)
        assert attempt is not None and job is not None
        attempt.state = "stopped"
        job.state = "queued"
        job.queue_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    transport = SimulatedTransport(controller_db)
    await TrainingController(transport, sessions=controller_db.begin).run_once()
    async with controller_db() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None and job.state == "failed" and job.safe_reason == "queue_timeout"
    assert not transport.deliveries and transport.status_calls == 0


async def final_checkpoint(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """Synthetic metadata only. This fixture is not runtime or measurement evidence."""
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        assert job is not None and attempt is not None
        dataset_id = uuid.uuid4()
        resolved = ResolvedTrainingSpec.model_validate(
            {
                "spec": {
                    "model": {"model_id": job.model_id, "variant_id": job.base_variant_id},
                    "data": {
                        "loss_policy": "all_tokens",
                        "train": {
                            "datasets": [
                                {
                                    "dataset_id": dataset_id,
                                    "sample_count": 1,
                                    "mixture_proportion": 1.0,
                                }
                            ],
                            "epoch_samples": 1,
                        },
                        "validation": {"dataset_ids": [dataset_id]},
                    },
                    "parameterization": {"target_modules": ["self_attn.q_proj"]},
                    "optim": {"updates": 2},
                    "output": {"adapter_slug": "output"},
                },
                "base_manifest_sha256": DIGEST,
                "datasets": [
                    {
                        "dataset_id": dataset_id,
                        "analysis_id": uuid.uuid4(),
                        "source_sha256": DIGEST,
                        "split_sha256": DIGEST,
                        "analysis_sha256": DIGEST,
                    }
                ],
                "tokenizer_sha256": DIGEST,
                "template_sha256": DIGEST,
                "enable_thinking": False,
                "runtime_sha256": DIGEST,
                "worker_version": "fixture",
                "resource_envelope": {
                    "weight_bytes": 100,
                    "adapter_bytes": 10,
                    "optimizer_bytes": 20,
                    "activation_bytes": 30,
                    "buffer_bytes": 0,
                    "safety_bytes": 10,
                    "checkpoint_bytes": 20,
                    "evidence_sha256": DIGEST,
                },
            }
        )
        job.resolved_spec = resolved.model_dump(mode="json")
        # A committed final checkpoint is injected as metadata to isolate the
        # extraction reducer. Other integration tests test all-copy commitment.
        checkpoint = TrainingCheckpointRow(
            id=uuid.uuid4(),
            job_id=JOB,
            attempt_id=ATTEMPT,
            fence=1,
            completed_update=2,
            manifest_sha256=DIGEST,
            manifest={},
            total_bytes=3,
            state="committed",
        )
        session.add(checkpoint)
        await session.flush()
        job.state, job.latest_checkpoint_id = "finalizing", checkpoint.id
        attempt.state = "stopped"
        return checkpoint.id


async def test_final_extraction_intent_survives_restart_with_exact_identity(
    controller_db: async_sessionmaker[AsyncSession],
) -> None:
    checkpoint_id = await final_checkpoint(controller_db)

    class PendingExtraction(SimulatedTransport):
        async def extract_final_adapter(
            self, checkpoint_id: uuid.UUID, adapter_id: uuid.UUID, command_id: uuid.UUID
        ) -> TrainingArtifactManifest | None:
            self.deliveries.append(command_id)
            assert adapter_id == uuid.uuid5(checkpoint_id, "automatic-adapter")
            return None

    transport = PendingExtraction(controller_db)
    for _ in range(2):
        await TrainingController(transport, sessions=controller_db.begin).tick(JOB)
    assert transport.deliveries == [uuid.uuid5(checkpoint_id, "automatic-extraction")] * 2
    async with controller_db() as session:
        rows = (await session.scalars(select(TrainingCommandRow))).all()
        assert len(rows) == 1 and rows[0].state == "dispatching"
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None and job.state == "finalizing" and job.adapter_id is None


async def test_cancel_wins_before_final_extraction_and_blocks_output(
    controller_db: async_sessionmaker[AsyncSession],
) -> None:
    await final_checkpoint(controller_db)
    async with controller_db.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        job.state, job.safe_reason = "cancelling", "cancelled"
    transport = SimulatedTransport(controller_db)
    await TrainingController(transport, sessions=controller_db.begin).tick(JOB)
    async with controller_db() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None and job.state == "cancelled" and job.adapter_id is None
        assert not (await session.scalars(select(TrainingCommandRow))).all()


async def test_unavailable_final_extraction_has_a_persisted_sixty_second_deadline(
    controller_db: async_sessionmaker[AsyncSession],
) -> None:
    checkpoint_id = await final_checkpoint(controller_db)
    async with controller_db.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        session.add(
            TrainingCommandRow(
                id=uuid.uuid5(checkpoint_id, "automatic-extraction"),
                actor_user_id=job.owner_user_id,
                idempotency_key="final",
                operation="training.final.extract",
                subject_id="final",
                job_id=JOB,
                request_sha256=DIGEST,
                payload={},
                state="dispatching",
                created_at=datetime.now(UTC) - timedelta(seconds=61),
            )
        )
    transport = SimulatedTransport(controller_db)
    await TrainingController(transport, sessions=controller_db.begin).tick(JOB)
    async with controller_db() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None and job.state == "failed" and job.safe_reason == "replication_failed"
    assert not transport.deliveries


async def test_two_rank_cancel_holds_both_nodes_until_every_rank_proves_stop(
    controller_db: async_sessionmaker[AsyncSession],
) -> None:
    async with controller_db.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        assert job is not None and attempt is not None
        job.state, job.safe_reason = "cancelling", "cancelled"
        attempt.world_size = 2
        node = NodeRow(
            id=uuid.uuid4(),
            name="coire-edge-b",
            role="studio",
            memory_total_bytes=10000,
            disk_total_bytes=10000,
            agent_version="fixture",
        )
        session.add(node)
        await session.flush()
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
                rank=1,
                reservation_id=hold.id,
                command_id=uuid.uuid4(),
                request_sha256=DIGEST,
                spawn_nonce=uuid.uuid4(),
                pid=456,
                process_create_time=datetime.now(UTC).timestamp(),
            )
        )

    class RankPartition(SimulatedTransport):
        blocked = True

        async def dispatch_command(self, command_id: uuid.UUID) -> None:
            async with self.factory() as session:
                row = await session.get(TrainingCommandRow, command_id)
                assert row is not None
                if row.subject_id == "coire-edge-b" and self.blocked:
                    raise TimeoutError("simulated rank partition")
            await super().dispatch_command(command_id)

        async def training_status(self, node: str, attempt_id: str) -> NodeTrainingStatus:
            if node == "coire-edge-b" and self.blocked:
                raise TimeoutError("simulated rank partition")
            return await super().training_status(node, attempt_id)

    transport = RankPartition(controller_db)
    transport.stop_available = True
    await TrainingController(transport, sessions=controller_db.begin).tick(JOB)
    async with controller_db() as session:
        holds = (await session.scalars(select(MemoryReservationRow))).all()
        assert len(holds) == 2 and all(hold.state == MemoryReservationState.HELD for hold in holds)
        proofs = (await session.scalars(select(TrainingParticipantRow.stop_proof))).all()
        assert sum(proof is not None for proof in proofs) == 1
    transport.blocked = False
    await TrainingController(transport, sessions=controller_db.begin).tick(JOB)
    async with controller_db() as session:
        holds = (await session.scalars(select(MemoryReservationRow))).all()
        assert len(holds) == 2 and all(
            hold.state == MemoryReservationState.RELEASED for hold in holds
        )
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None and job.state == "cancelled"


@pytest.mark.parametrize("origin", ["admin", "protective"])
async def test_paused_jobs_require_current_evidence_and_admin_pause_stays_manual(
    controller_db: async_sessionmaker[AsyncSession],
    origin: str,
) -> None:
    async with controller_db.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        assert job is not None and attempt is not None
        attempt.state = "stopped"
        job.state, job.pause_origin = "paused", origin
        job.updated_at = datetime.now(UTC) - timedelta(seconds=61)

    class NoMeasuredProfile(SimulatedTransport):
        profile_calls = 0

        async def resume_profile(self, job_id: str) -> TrainingProfile | None:
            self.profile_calls += 1
            return None

    transport = NoMeasuredProfile(controller_db)
    await TrainingController(transport, sessions=controller_db.begin).tick(JOB)
    async with controller_db() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None and job.state == "paused" and job.pause_origin == origin
        assert len((await session.scalars(select(TrainingAttemptRow))).all()) == 1
    assert transport.profile_calls == (0 if origin == "admin" else 1)
    assert not transport.deliveries
