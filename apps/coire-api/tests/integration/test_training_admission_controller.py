"""Postgres races for training eviction, reverse admission and restoration."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_training_transactions import bound_submission
from test_training_transactions import database as database
from training_measurement_fixtures import persist_measured_profile

from coire_api.db import (
    EngineProcessRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelRow,
    ModelVariantRow,
    NodeMemoryLedgerRow,
    NodeRow,
    PlacementCommandRow,
    TrainingAttemptRow,
    TrainingEvictionIntentRow,
    TrainingJobRow,
)
from coire_api.instance.service import transition
from coire_api.placement.service import (
    LedgerNotFoundError,
    acquire_lease,
    node_admission_locks,
    training_allows_work,
)
from coire_api.training.service import submit_training
from coire_core.errors import TrainingConflict
from coire_core.models.engine import EngineState
from coire_core.models.instance import InstanceState
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.registry import ModelState
from coire_core.models.training_node import TrainingStopReceipt
from coire_scheduler.training import enqueue_attempt_commands
from coire_scheduler.training_admission import admit_training, victim_drain_barrier
from coire_scheduler.training_recovery import record_stop_proof, restore_evictions

pytestmark = pytest.mark.integration


async def occupied_job(
    factory: async_sessionmaker[AsyncSession], *, pinned: bool = False
) -> tuple[str, uuid.UUID, uuid.UUID]:
    principal, body, resolved = await bound_submission(factory)
    async with factory.begin() as session:
        node = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-a"))
        assert node is not None and principal.user_id is not None
        variant = await session.get(ModelVariantRow, resolved.spec.model.variant_id)
        assert variant is not None
        variant.validated = True
        resolved = await persist_measured_profile(session, principal.user_id, resolved, [node])
        receipt = await submit_training(session, principal, body, "eviction", resolved=resolved)
        instance = ModelInstanceRow(
            id=uuid.uuid4(),
            model_id=resolved.spec.model.model_id,
            variant_id=resolved.spec.model.variant_id,
            policy="single:coire-edge-a",
            state=InstanceState.READY,
        )
        session.add(instance)
        await session.flush()
        engine = EngineProcessRow(
            id=uuid.uuid4(),
            instance_id=instance.id,
            model_id=instance.model_id,
            variant_id=instance.variant_id,
            node_id=node.id,
            port=12345,
            pid=123,
            process_create_time=datetime.now(UTC).timestamp(),
            state=EngineState.READY,
            estimate_bytes=600,
            resident_bytes=600,
            last_health_at=datetime.now(UTC),
        )
        hold = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node.id,
            holder_type=ReservationHolder.MODEL,
            holder_id=str(instance.id),
            bytes=600,
            pinned=pinned,
            state=MemoryReservationState.HELD,
            last_used_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        session.add_all([engine, hold])
        await session.flush()
        session.add(
            InstanceMemberRow(
                instance_id=instance.id,
                node_id=node.id,
                rank=0,
                engine_id=engine.id,
                reservation_id=hold.id,
                host=node.name,
                port=12345,
            )
        )
        session.add(
            NodeMemoryLedgerRow(
                node_id=node.id,
                budget_bytes=1000,
                sandbox_bytes=0,
                measured_resident_bytes=600,
                thermal_state="nominal",
                health="healthy",
                health_sampled_at=datetime.now(UTC),
            )
        )
        return receipt.job_id, node.id, hold.id


async def admit(
    factory: async_sessionmaker[AsyncSession], job_id: str, node_id: uuid.UUID
) -> str | None:
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, job_id)
        node = await session.get(NodeRow, node_id)
        assert job is not None and node is not None
        attempt = await admit_training(session, job, [node])
        return attempt.id if attempt else None


async def confirm_eviction(factory: async_sessionmaker[AsyncSession], attempt_id: str) -> None:
    """Synthetic authenticated unload reduction, never a timeout-based release."""
    async with factory.begin() as session:
        victim = await session.scalar(
            select(TrainingEvictionIntentRow).where(
                TrainingEvictionIntentRow.attempt_id == attempt_id
            )
        )
        assert victim is not None
        hold = await session.get(MemoryReservationRow, victim.reservation_id)
        command = await session.get(PlacementCommandRow, victim.id)
        assert hold is not None and command is not None
        engine = await session.get(EngineProcessRow, command.engine_id)
        assert engine is not None
        engine.state = EngineState.STOPPED
        command.state = "succeeded"
        hold.state, hold.released_at = MemoryReservationState.RELEASED, datetime.now(UTC)
        await transition(
            session, victim.instance_id, InstanceState.STOPPED, reason="simulated confirmed unload"
        )
        ledger = await session.get(NodeMemoryLedgerRow, hold.node_id)
        assert ledger is not None
        ledger.measured_resident_bytes = 0


async def test_atomic_idle_drain_keeps_both_holds_and_blocks_prepare_until_confirmed(
    database: async_sessionmaker[AsyncSession],
) -> None:
    job_id, node_id, victim_id = await occupied_job(database)
    attempt_id = await admit(database, job_id, node_id)
    assert attempt_id is not None
    async with database.begin() as session:
        assert not await victim_drain_barrier(session, attempt_id)
        assert await enqueue_attempt_commands(session, attempt_id, "prepare") == []
        holds = (
            await session.scalars(
                select(MemoryReservationRow).where(MemoryReservationRow.node_id == node_id)
            )
        ).all()
        assert sum(hold.bytes for hold in holds) == 770
        assert {hold.state for hold in holds} == {
            MemoryReservationState.RELEASING,
            MemoryReservationState.PENDING,
        }
        victim_hold = await session.get(MemoryReservationRow, victim_id)
        assert victim_hold is not None and victim_hold.state == MemoryReservationState.RELEASING
    await confirm_eviction(database, attempt_id)
    async with database.begin() as session:
        assert await victim_drain_barrier(session, attempt_id)
        commands = await enqueue_attempt_commands(session, attempt_id, "prepare")
        assert len(commands) == 1 and commands[0].operation == "node.training.prepare"


async def test_training_drain_wins_lease_race_under_the_shared_node_lock(
    database: async_sessionmaker[AsyncSession],
) -> None:
    job_id, node_id, victim_id = await occupied_job(database)
    acquired, release = asyncio.Event(), asyncio.Event()

    async def training() -> None:
        async with database.begin() as session:
            job, node = (
                await session.get(TrainingJobRow, job_id),
                await session.get(NodeRow, node_id),
            )
            assert job is not None and node is not None
            assert await admit_training(session, job, [node]) is not None
            acquired.set()
            await release.wait()

    async def lease() -> None:
        await acquired.wait()
        async with database.begin() as session:
            await acquire_lease(session, victim_id, "lease-race", ttl_seconds=30)

    training_task = asyncio.create_task(training())
    lease_task = asyncio.create_task(lease())
    await acquired.wait()
    await asyncio.sleep(0.05)
    assert not lease_task.done()
    release.set()
    await training_task
    with pytest.raises(LedgerNotFoundError):
        await lease_task


@pytest.mark.parametrize("protected", ["pin", "lease"])
async def test_protected_serving_residency_is_not_evicted_without_a_measured_mix(
    database: async_sessionmaker[AsyncSession],
    protected: str,
) -> None:
    job_id, node_id, victim_id = await occupied_job(database, pinned=protected == "pin")
    if protected == "lease":
        async with database.begin() as session:
            await acquire_lease(session, victim_id, "active", ttl_seconds=30)
    assert await admit(database, job_id, node_id) is None
    async with database() as session:
        assert not (await session.scalars(select(TrainingEvictionIntentRow))).all()
        hold = await session.get(MemoryReservationRow, victim_id)
        assert hold is not None and hold.state == MemoryReservationState.HELD


@pytest.mark.parametrize("change", [None, "pin", "pin-cycle", "placement", "retire"])
async def test_restoration_offers_once_and_preserves_newer_administrator_decisions(
    database: async_sessionmaker[AsyncSession],
    change: str | None,
) -> None:
    job_id, node_id, _ = await occupied_job(database)
    attempt_id = await admit(database, job_id, node_id)
    assert attempt_id is not None
    await confirm_eviction(database, attempt_id)
    async with database.begin() as session:
        intent = await session.scalar(
            select(TrainingEvictionIntentRow).where(
                TrainingEvictionIntentRow.attempt_id == attempt_id
            )
        )
        job = await session.get(TrainingJobRow, job_id)
        assert intent is not None and job is not None
        job.state, job.safe_reason = "cancelling", "cancelled"
        hold = await session.get(MemoryReservationRow, intent.reservation_id)
        instance = await session.get(ModelInstanceRow, intent.instance_id)
        assert hold is not None and instance is not None
        if change == "pin":
            hold.pinned = True
        elif change == "pin-cycle":
            from coire_api.placement.service import set_pin
            from coire_core.models.placement import PinUpdate

            await set_pin(session, hold.id, PinUpdate(pinned=True), actor="fixture-admin")
            await set_pin(session, hold.id, PinUpdate(pinned=False), actor="fixture-admin")
        elif change == "placement":
            instance.policy = "single:coire-edge-b"
        elif change == "retire":
            model = await session.get(ModelRow, instance.model_id)
            assert model is not None
            model.state = ModelState.RETIRED
        assert await record_stop_proof(
            session,
            attempt_id,
            TrainingStopReceipt(
                attempt_id=attempt_id,
                fence=job.fence,
                node="coire-edge-a",
                stopped=True,
                observed_at=datetime.now(UTC),
            ),
        )
    async with database.begin() as session:
        await restore_evictions(session, attempt_id)
        intent = await session.scalar(
            select(TrainingEvictionIntentRow).where(
                TrainingEvictionIntentRow.attempt_id == attempt_id
            )
        )
        assert intent is not None
        assert intent.restoration_state == ("offered" if change is None else "superseded")
        replacements = (
            await session.scalars(
                select(ModelInstanceRow).where(ModelInstanceRow.state == InstanceState.REQUESTED)
            )
        ).all()
        assert len(replacements) == (1 if change is None else 0)


async def test_pending_training_holds_block_reverse_loads_shards_and_image_dispatch(
    database: async_sessionmaker[AsyncSession],
) -> None:
    from coire_scheduler.image_dispatch import _image_busy

    job_id, node_id, _ = await occupied_job(database)
    assert await admit(database, job_id, node_id) is not None
    async with database.begin() as session:
        assert not await training_allows_work(session, [node_id])
        job = await session.get(TrainingJobRow, job_id)
        assert job is not None
        assert await _image_busy(session, node_id, job.model_id)
        with pytest.raises(TrainingConflict):
            async with session.begin_nested(), node_admission_locks(session, [node_id]):
                session.add(
                    MemoryReservationRow(
                        id=uuid.uuid4(),
                        node_id=node_id,
                        holder_type=ReservationHolder.MODEL,
                        holder_id="reverse-load",
                        bytes=1,
                        state=MemoryReservationState.PENDING,
                    )
                )
                await session.flush()


async def test_measured_profile_preserves_actual_instance_multiplicity(
    database: async_sessionmaker[AsyncSession],
) -> None:
    from coire_core.models.training import ResolvedTrainingSpec
    from coire_scheduler.training_guard import (
        current_residents,
        instance_latency_reason,
        matching_profile,
    )

    job_id, node_id, _ = await occupied_job(database, pinned=True)
    async with database.begin() as session:
        job = await session.get(TrainingJobRow, job_id)
        node = await session.get(NodeRow, node_id)
        assert job is not None and node is not None
        residents = await current_residents(session, [node_id])
        assert residents is not None and len(residents) == 1
        resolved = ResolvedTrainingSpec.model_validate(job.resolved_spec)
        await persist_measured_profile(
            session, job.owner_user_id, resolved, [node], residents=residents
        )
        assert await matching_profile(session, job, [node], residents=residents) is not None
        assert await instance_latency_reason(session, residents[0]) == "insufficient_samples"
        # A second actual instance of the same exact base remains a second
        # workload identity. No parent-model or variant set collapse is allowed.
        original = await session.get(ModelInstanceRow, residents[0].instance_id)
        assert original is not None
        second = ModelInstanceRow(
            id=uuid.uuid4(),
            model_id=original.model_id,
            variant_id=original.variant_id,
            policy="single:coire-edge-a",
            state=InstanceState.READY,
        )
        session.add(second)
        await session.flush()
        hold = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node_id,
            holder_type=ReservationHolder.MODEL,
            holder_id=str(second.id),
            bytes=100,
            pinned=True,
            state=MemoryReservationState.HELD,
        )
        session.add(hold)
        await session.flush()
        session.add(
            InstanceMemberRow(
                instance_id=second.id,
                node_id=node_id,
                rank=0,
                reservation_id=hold.id,
                host=node.name,
                port=12346,
            )
        )
        await session.flush()
        current = await current_residents(session, [node_id])
        assert current is not None and len(current) == 2
        assert current[0].target == current[1].target
        assert await matching_profile(session, job, [node], residents=current) is None


@pytest.mark.parametrize("fault", ["memory", "thermal", "stale"])
async def test_public_guard_uses_fresh_physical_observations_and_never_fake_health(
    database: async_sessionmaker[AsyncSession],
    fault: str,
) -> None:
    from coire_api.db import TrainingParticipantRow
    from coire_scheduler.training_guard import guard_reason

    job_id, node_id, _ = await occupied_job(database)
    attempt_id = await admit(database, job_id, node_id)
    assert attempt_id is not None
    await confirm_eviction(database, attempt_id)
    async with database.begin() as session:
        p = await session.scalar(
            select(TrainingParticipantRow).where(TrainingParticipantRow.attempt_id == attempt_id)
        )
        ledger = await session.get(NodeMemoryLedgerRow, node_id)
        assert p is not None and ledger is not None
        p.footprint_bytes = 171 if fault == "memory" else 170
        if fault == "thermal":
            ledger.thermal_state = "critical"
        if fault == "stale":
            ledger.health_sampled_at = datetime.now(UTC) - timedelta(seconds=61)
        assert (
            await guard_reason(attempt_id, session=session)
            == {
                "memory": "memory_breach",
                "thermal": "thermal_breach",
                "stale": "insufficient_samples",
            }[fault]
        )


async def test_pin_transaction_wins_admission_race_without_lost_update(
    database: async_sessionmaker[AsyncSession],
) -> None:
    from coire_api.placement.service import set_pin
    from coire_core.models.placement import PinUpdate

    job_id, node_id, victim_id = await occupied_job(database)
    acquired, release = asyncio.Event(), asyncio.Event()

    async def pin() -> None:
        async with database.begin() as session:
            await set_pin(session, victim_id, PinUpdate(pinned=True), actor="fixture-admin")
            acquired.set()
            await release.wait()

    async def training() -> str | None:
        await acquired.wait()
        return await admit(database, job_id, node_id)

    pin_task, train_task = asyncio.create_task(pin()), asyncio.create_task(training())
    await asyncio.wait_for(acquired.wait(), timeout=10)
    await asyncio.sleep(0.05)
    assert not train_task.done()
    release.set()
    await pin_task
    assert await train_task is None
    async with database() as session:
        hold = await session.get(MemoryReservationRow, victim_id)
        assert hold is not None and hold.pinned and hold.state == MemoryReservationState.HELD
        assert not (await session.scalars(select(TrainingEvictionIntentRow))).all()


async def test_training_transaction_wins_reverse_model_allocation_race(
    database: async_sessionmaker[AsyncSession],
) -> None:
    job_id, node_id, _ = await occupied_job(database)
    acquired, release = asyncio.Event(), asyncio.Event()

    async def training() -> None:
        async with database.begin() as session:
            job, node = (
                await session.get(TrainingJobRow, job_id),
                await session.get(NodeRow, node_id),
            )
            assert job is not None and node is not None
            assert await admit_training(session, job, [node]) is not None
            acquired.set()
            await release.wait()

    async def model_load() -> None:
        await acquired.wait()
        async with database.begin() as session:
            session.add(
                MemoryReservationRow(
                    id=uuid.uuid4(),
                    node_id=node_id,
                    holder_type=ReservationHolder.MODEL,
                    holder_id="competing-model",
                    bytes=1,
                    state=MemoryReservationState.PENDING,
                )
            )
            await session.flush()

    train_task, load_task = asyncio.create_task(training()), asyncio.create_task(model_load())
    await asyncio.wait_for(acquired.wait(), timeout=10)
    await asyncio.sleep(0.05)
    assert not load_task.done()
    release.set()
    await train_task
    with pytest.raises(TrainingConflict):
        await load_task


async def test_two_node_competing_admissions_hold_full_envelopes_atomically(
    database: async_sessionmaker[AsyncSession],
) -> None:
    import yaml

    from coire_api.db import LinkObservationRow
    from coire_core.models.sharding import ProbeOutcome, ProbeTransport
    from coire_core.models.training import TrainingSpec, TrainingSubmission

    principal, _, resolved = await bound_submission(database)
    raw = resolved.spec.model_dump(mode="json")
    raw["placement"] = {"mode": "data_parallel"}
    raw["optim"]["batch_size"] = 2
    raw["data"]["train"].update(epoch_samples=2, replacement=True)
    spec = TrainingSpec.model_validate(raw)
    resolved = resolved.model_copy(update={"spec": spec})
    async with database.begin() as session:
        nodes = list((await session.scalars(select(NodeRow).order_by(NodeRow.name))).all())
        assert len(nodes) == 2 and principal.user_id is not None
        resolved = await persist_measured_profile(session, principal.user_id, resolved, nodes)
        first = await submit_training(
            session,
            principal,
            TrainingSubmission(source_yaml=yaml.safe_dump(spec.model_dump(mode="json"))),
            "dp-first",
            resolved=resolved,
        )
        second_spec = spec.model_copy(
            update={"output": spec.output.model_copy(update={"adapter_slug": "dp-second"})}
        )
        second = await submit_training(
            session,
            principal,
            TrainingSubmission(source_yaml=yaml.safe_dump(second_spec.model_dump(mode="json"))),
            "dp-second",
            resolved=resolved.model_copy(update={"spec": second_spec}),
        )
        for node in nodes:
            session.add(
                NodeMemoryLedgerRow(
                    node_id=node.id,
                    budget_bytes=500,
                    sandbox_bytes=0,
                    measured_resident_bytes=0,
                    thermal_state="nominal",
                    health="healthy",
                    health_sampled_at=datetime.now(UTC),
                )
            )
        for age in (2, 1, 0):
            session.add(
                LinkObservationRow(
                    id=uuid.uuid4(),
                    node_a_id=nodes[0].id,
                    node_b_id=nodes[1].id,
                    transport=ProbeTransport.JACCL,
                    outcome=ProbeOutcome.SUCCEEDED,
                    bandwidth_bytes_per_second=1_000_000_000,
                    latency_ms=1,
                    os_version_a="26.2",
                    os_version_b="26.2",
                    engine_version="fixture",
                    observed_at=datetime.now(UTC) - timedelta(seconds=age),
                )
            )
        ids = [node.id for node in nodes]

    async def group_admit(job_id: str, node_ids: list[uuid.UUID]) -> str | None:
        async with database.begin() as session:
            job = await session.get(TrainingJobRow, job_id)
            participants = [await session.get(NodeRow, node_id) for node_id in node_ids]
            assert job is not None and all(node is not None for node in participants)
            attempt = await admit_training(
                session, job, [node for node in participants if node is not None]
            )
            return attempt.id if attempt else None

    results = await asyncio.gather(
        group_admit(first.job_id, ids), group_admit(second.job_id, list(reversed(ids)))
    )
    assert sum(result is not None for result in results) == 1
    async with database() as session:
        holds = (
            await session.scalars(
                select(MemoryReservationRow).where(
                    MemoryReservationRow.holder_type == ReservationHolder.TRAINING
                )
            )
        ).all()
        assert len(holds) == 2 and {hold.node_id for hold in holds} == set(ids)
        assert all(hold.bytes == resolved.resource_envelope.memory_bytes for hold in holds)
        assert all(hold.state == MemoryReservationState.PENDING for hold in holds)


@pytest.mark.parametrize("stale", [False, True])
async def test_authenticated_probe_accounts_training_footprint_and_preserves_sample_age(
    database: async_sessionmaker[AsyncSession],
    stale: bool,
) -> None:
    from coire_api.db import TrainingParticipantRow
    from coire_api.nodes_prober import NodeProber
    from coire_core.models.node import NodeStatus, NodeStatusV2, Reachability
    from coire_core.net import ControlClient
    from coire_core.settings import Settings
    from coire_scheduler.training_guard import guard_reason

    job_id, node_id, _ = await occupied_job(database)
    attempt_id = await admit(database, job_id, node_id)
    assert attempt_id is not None
    await confirm_eviction(database, attempt_id)
    sampled = datetime.now(UTC) - timedelta(seconds=61 if stale else 0)

    class RecordedProbe(NodeProber):
        async def _probe_node(
            self, client: ControlClient, row: NodeRow, token: str
        ) -> NodeStatus | NodeStatusV2 | None:
            row.reachability = Reachability.HEALTHY
            return NodeStatusV2.model_validate(
                {
                    "name": row.name,
                    "agent_version": "fixture",
                    "uptime_seconds": 1,
                    "cpu_percent": 0,
                    "thermal_state": "nominal",
                    "memory_total_bytes": 10000,
                    "memory_free_bytes": 9000,
                    "disk_total_bytes": 10000,
                    "disk_free_bytes": 9000,
                    "agent_cpu_percent": 0,
                    "agent_rss_bytes": 1,
                    "collection_budget_ok": True,
                    "sampled_at": sampled,
                    "training": [
                        {
                            "attempt_id": attempt_id,
                            "job_id": job_id,
                            "fence": 1,
                            "node": row.name,
                            "liveness": "running",
                            "update": 0,
                            "footprint_bytes": 270,
                            "lease_expires_at": datetime.now(UTC) + timedelta(seconds=30),
                        }
                    ]
                    if row.id == node_id
                    else [],
                }
            )

    await RecordedProbe(Settings())._probe_once(database)
    async with database.begin() as session:
        ledger = await session.get(NodeMemoryLedgerRow, node_id)
        participant = await session.scalar(
            select(TrainingParticipantRow).where(TrainingParticipantRow.attempt_id == attempt_id)
        )
        assert ledger is not None and participant is not None
        assert ledger.measured_resident_bytes == 270 and participant.footprint_bytes == 270
        assert ledger.health_sampled_at == sampled
        assert await guard_reason(attempt_id, session=session) == (
            "insufficient_samples" if stale else "memory_breach"
        )


async def test_existing_measured_chat_keeps_priority_without_fabricated_live_latency(
    database: async_sessionmaker[AsyncSession],
) -> None:
    from coire_api.db import TrainingCommandRow, TrainingParticipantRow, TrainingProfileRow
    from coire_api.training.service import training_id
    from coire_core.models.training import ResolvedTrainingSpec
    from coire_scheduler.training_guard import (
        current_residents,
        instance_latency_reason,
        matching_profile,
    )

    job_id, node_id, model_hold = await occupied_job(database, pinned=True)
    async with database.begin() as session:
        job, node = await session.get(TrainingJobRow, job_id), await session.get(NodeRow, node_id)
        assert job is not None and node is not None
        residents = await current_residents(session, [node_id])
        assert residents is not None and len(residents) == 1
        await persist_measured_profile(
            session,
            job.owner_user_id,
            ResolvedTrainingSpec.model_validate(job.resolved_spec),
            [node],
            residents=residents,
        )
        profile = await matching_profile(session, job, [node], residents=residents)
        assert profile is not None
        # Simulate an already-admitted native attempt. New mixed admission is
        # independently required to have live sample eligibility.
        attempt_id = training_id()
        job.state, job.fence = "running", 1
        session.add(
            TrainingAttemptRow(
                id=attempt_id,
                job_id=job_id,
                generation=1,
                fence=1,
                world_size=1,
                runtime_sha256=ResolvedTrainingSpec.model_validate(
                    job.resolved_spec
                ).runtime_sha256,
                state="running",
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            )
        )
        await session.flush()
        hold = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node_id,
            holder_type=ReservationHolder.TRAINING,
            holder_id=attempt_id,
            bytes=170,
            state=MemoryReservationState.HELD,
        )
        session.add(hold)
        await session.flush()
        session.add(
            TrainingParticipantRow(
                attempt_id=attempt_id,
                node_id=node_id,
                rank=0,
                reservation_id=hold.id,
                command_id=uuid.uuid4(),
                request_sha256=job.resolved_sha256,
                spawn_nonce=uuid.uuid4(),
                footprint_bytes=170,
            )
        )
        session.add(
            TrainingCommandRow(
                id=uuid.uuid5(uuid.NAMESPACE_URL, f"coire:admission-evidence:{attempt_id}"),
                actor_user_id=job.owner_user_id,
                idempotency_key=f"scope:{attempt_id}",
                operation="training.admission.evidence",
                subject_id=attempt_id,
                job_id=job_id,
                attempt_id=attempt_id,
                payload={"profile_id": str(profile.id), "swap_bytes": {str(node_id): 0}},
                request_sha256="a" * 64,
                state="succeeded",
            )
        )
        await session.flush()
        assert await instance_latency_reason(session, residents[0]) == "insufficient_samples"
        await acquire_lease(session, model_hold, "protected-chat", ttl_seconds=30)
        row = await session.get(TrainingProfileRow, profile.id)
        attempt = await session.get(TrainingAttemptRow, attempt_id)
        assert row is not None and attempt is not None
        row.invalidated_reason = "latency_breach"
        attempt.state, job.state = "stopping", "pausing"
        await session.flush()
        await acquire_lease(session, model_hold, "chat-during-protective-stop", ttl_seconds=30)
        assert not await training_allows_work(session, [node_id])


async def test_training_drain_is_not_dispatched_as_model_placement(
    database: async_sessionmaker[AsyncSession],
) -> None:
    from coire_api.db import PlacementDecisionRow
    from coire_core.models.placement import PlacementState
    from coire_scheduler.main import pending_placement_ids

    job_id, node_id, _ = await occupied_job(database)
    attempt_id = await admit(database, job_id, node_id)
    assert attempt_id is not None
    async with database.begin() as session:
        job = await session.get(TrainingJobRow, job_id)
        assert job is not None
        ordinary = PlacementDecisionRow(
            id=uuid.uuid4(),
            model_id=job.model_id,
            variant_id=job.base_variant_id,
            policy="single:auto",
            required_bytes=1,
            state=PlacementState.REQUESTED,
        )
        session.add(ordinary)
        await session.flush()
        selected = await pending_placement_ids(session)
        assert selected == [ordinary.id]
        assert not await victim_drain_barrier(session, attempt_id)


async def test_controller_does_not_probe_native_status_before_eviction_barrier(
    database: async_sessionmaker[AsyncSession],
) -> None:
    from coire_scheduler.training_controller import TrainingController

    job_id, node_id, _ = await occupied_job(database)
    attempt_id = await admit(database, job_id, node_id)
    assert attempt_id is not None

    class NoNativeStatus:
        async def training_status(self, node: str, attempt_id: str) -> None:
            raise AssertionError("no native attempt exists before preparation")

    controller = TrainingController(NoNativeStatus(), sessions=database.begin)  # type: ignore[arg-type]
    await controller._observe_node("coire-edge-a", attempt_id)
    async with database() as session:
        job = await session.get(TrainingJobRow, job_id)
        attempt = await session.get(TrainingAttemptRow, attempt_id)
        assert job is not None and job.state == "reserving"
        assert attempt is not None and attempt.state == "preparing"
