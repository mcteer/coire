"""Shared ledger fences preserve training and protected serving ownership."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from evaluation_fixtures import FIXTURE, seed_evaluation
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import (
    Base,
    EngineProcessRow,
    EvaluationAttemptRow,
    EvaluationCoexistenceProfileRow,
    EvaluationMeasurementRow,
    EvaluationRunRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelRow,
    ModelVariantRow,
    NodeMemoryLedgerRow,
    NodeRow,
    VariantCopyRow,
)
from coire_core.models.acquisition import VariantState
from coire_core.models.engine import EngineState
from coire_core.models.evaluation import EvaluationWorkload
from coire_core.models.instance import InstanceState
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.registry import CopyRole, ModelState
from coire_core.settings import Settings
from coire_scheduler.evaluation_admission import reserve_phase
from coire_scheduler.evaluation_guard import guard_reason

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("blocked_by", ["stale", "swap", "thermal", "training", "resident", "core"])
async def test_refusal_allocates_nothing_and_keeps_existing_holds(
    training_postgres_url: str, tmp_path: Path, blocked_by: str
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            identity = await seed_evaluation(session)
            run = await session.get(EvaluationRunRow, identity)
            assert run is not None
            work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
            session.add(
                ModelRow(
                    id=work.target.target.model_id,
                    repo_id="synthetic/evaluation",
                    slug="synthetic-evaluation",
                    display_name="Fixture",
                    state=ModelState.READY,
                    visibility="admin_only",
                    placement_policy="single:auto",
                    memory_estimate_bytes=1024,
                    idle_ttl_seconds=900,
                    precision="bf16",
                    weight_bytes=1024,
                    total_bytes=1024,
                    file_count=2,
                )
            )
            await session.flush()
            session.add(
                ModelVariantRow(
                    id=work.target.target.variant_id,
                    model_id=work.target.target.model_id,
                    name="fixture",
                    slug="evaluation-fixture",
                    precision="bf16",
                    source_revision="synthetic",
                    memory_estimate_bytes=1024,
                    state=VariantState.READY,
                    validated=True,
                )
            )
            node = NodeRow(
                id=uuid.uuid4(),
                name="coire-edge-a",
                role=NodeRole.CORE if blocked_by == "core" else NodeRole.STUDIO,
                reachability=Reachability.HEALTHY,
                memory_total_bytes=128 * 1024**3,
                disk_total_bytes=1024**4,
                gpu_cores=60,
                agent_version="fixture",
            )
            session.add(node)
            await session.flush()
            session.add(
                VariantCopyRow(
                    variant_id=work.target.target.variant_id,
                    node_id=node.id,
                    path="/synthetic/fixture",
                    bytes=1024,
                    manifest_sha256=work.target.target.base_manifest_sha256,
                    verified=True,
                    verified_at=datetime.now(UTC),
                    role=CopyRole.ORIGIN,
                )
            )
            session.add(
                NodeMemoryLedgerRow(
                    node_id=node.id,
                    budget_bytes=64 * 1024**3,
                    measured_resident_bytes=1024**3,
                    swap_used_bytes=1 if blocked_by == "swap" else 0,
                    thermal_state="critical" if blocked_by == "thermal" else "nominal",
                    health=Reachability.HEALTHY,
                    health_sampled_at=datetime.now(UTC)
                    - timedelta(seconds=120 if blocked_by == "stale" else 1),
                )
            )
            hold = None
            if blocked_by in {"training", "resident"}:
                holder = uuid.uuid4()
                hold = MemoryReservationRow(
                    id=uuid.uuid4(),
                    node_id=node.id,
                    holder_type=ReservationHolder.TRAINING
                    if blocked_by == "training"
                    else ReservationHolder.MODEL,
                    holder_id=str(holder),
                    bytes=4 * 1024**3,
                    pinned=True,
                    state=MemoryReservationState.HELD,
                )
                session.add(hold)
                await session.flush()
                if blocked_by == "resident":
                    instance = ModelInstanceRow(
                        id=holder,
                        model_id=work.target.target.model_id,
                        variant_id=work.target.target.variant_id,
                        policy="single:coire-edge-a",
                        state=InstanceState.READY,
                    )
                    session.add(instance)
                    await session.flush()
                    process = EngineProcessRow(
                        id=uuid.uuid4(),
                        instance_id=instance.id,
                        model_id=instance.model_id,
                        variant_id=instance.variant_id,
                        node_id=node.id,
                        port=9999,
                        pid=5,
                        process_create_time=1,
                        state=EngineState.READY,
                        estimate_bytes=hold.bytes,
                    )
                    session.add(process)
                    await session.flush()
                    session.add(
                        InstanceMemberRow(
                            instance_id=instance.id,
                            node_id=node.id,
                            rank=0,
                            host="127.0.0.1",
                            port=9999,
                            engine_id=process.id,
                            reservation_id=hold.id,
                        )
                    )
            attempt = EvaluationAttemptRow(
                id=work.attempt_id,
                run_id=run.id,
                phase="base",
                ordinal=1,
                fence=1,
                workload=work.model_dump(mode="json"),
                target_sha256="a" * 64,
                deadline_at=work.deadline,
                state="pending",
            )
            session.add(attempt)
            await session.commit()
            before = await session.scalar(select(func.count()).select_from(MemoryReservationRow))
            assert not await reserve_phase(
                session,
                run,
                attempt,
                work.target,
                Settings(evaluations_enabled=True, training_dataset_dir=str(tmp_path)),
            )
            await session.commit()
            assert attempt.sandbox_reservation_id is None and attempt.instance_id is None
            assert (
                await session.scalar(select(func.count()).select_from(MemoryReservationRow))
                == before
            )
            if hold is not None:
                await session.refresh(hold)
                assert hold.state is MemoryReservationState.HELD and hold.pinned
    finally:
        await engine.dispose()


async def test_live_guard_invalidates_its_profile_on_stale_telemetry(
    training_postgres_url: str,
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            identity = await seed_evaluation(session)
            run = await session.get(EvaluationRunRow, identity)
            assert run is not None
            node = NodeRow(
                id=uuid.uuid4(),
                name="coire-edge-a",
                role=NodeRole.STUDIO,
                reachability=Reachability.HEALTHY,
                memory_total_bytes=128 * 1024**3,
                disk_total_bytes=1024**4,
                gpu_cores=60,
                agent_version="fixture",
            )
            session.add(node)
            measurement = EvaluationMeasurementRow(
                id=uuid.uuid4(), owner_user_id=run.owner_user_id, request={}, state="succeeded"
            )
            session.add(measurement)
            await session.flush()
            profile = EvaluationCoexistenceProfileRow(
                id=uuid.uuid4(),
                measurement_id=measurement.id,
                fingerprint_sha256="a" * 64,
                report_sha256="b" * 64,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            session.add(profile)
            await session.flush()
            work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
            attempt = EvaluationAttemptRow(
                id=work.attempt_id,
                run_id=run.id,
                phase="base",
                ordinal=1,
                fence=1,
                workload=work.model_dump(mode="json"),
                target_sha256="a" * 64,
                deadline_at=work.deadline,
                state="running",
                profile_id=profile.id,
                node_id=node.id,
            )
            session.add(attempt)
            await session.commit()
            assert await guard_reason(session, attempt) == "telemetry_stale"
            await session.commit()
            await session.refresh(profile)
            assert (
                profile.invalidated_at is not None
                and profile.invalidated_reason == "telemetry_stale"
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "fault", [None, "missing_sample", "foreign_resident", "swap", "tampered", "invalidated"]
)
async def test_profile_matches_only_complete_persisted_qualification(
    training_postgres_url: str, fault: str | None
) -> None:
    from coire_core.models.evaluation import (
        EvaluationLatency,
        EvaluationMeasurement,
        EvaluationMeasurementRequest,
        EvaluationResident,
        EvaluationSubject,
        EvaluationSubmission,
    )
    from coire_scheduler.evaluation_guard import matching_profile, report_digest

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            run_id = await seed_evaluation(session)
            run = await session.get(EvaluationRunRow, run_id)
            assert run is not None
            work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
            resident = EvaluationResident(instance_id=uuid.uuid4(), target=work.target.target)
            request = EvaluationMeasurementRequest(
                evaluation=EvaluationSubmission(
                    suite_id="task-recovery",
                    suite_version=1,
                    subjects=[
                        EvaluationSubject(
                            model_id=resident.target.model_id, variant_id=resident.target.variant_id
                        )
                    ],
                ),
                node="coire-edge-a",
                resident_targets=[resident],
                prompt_set_sha256="a" * 64,
            )
            now = datetime.now(UTC)
            identity = uuid.uuid4()
            report = EvaluationMeasurement(
                id=identity,
                version=1,
                state="succeeded",
                request=request,
                targets=[
                    EvaluationLatency(
                        instance_id=resident.instance_id,
                        baseline_requests=100,
                        mixed_requests=100,
                        baseline_p95_seconds=1,
                        mixed_p95_seconds=1.2,
                        gateway_p95_seconds=0.01,
                        failures=0,
                    )
                ],
                thermal_ok=True,
                created_at=now,
                valid_until=now + timedelta(hours=24),
                profile_sha256="b" * 64,
                report_sha256="0" * 64,
            )
            report = report.model_copy(update={"report_sha256": report_digest(report)})
            document = report.model_dump(mode="json")
            if fault == "missing_sample":
                document["targets"][0]["mixed_requests"] = 99
            elif fault == "foreign_resident":
                document["targets"][0]["instance_id"] = str(uuid.uuid4())
            elif fault == "swap":
                document["swap_growth_bytes"] = 1
            elif fault == "tampered":
                document["targets"][0]["mixed_p95_seconds"] = 1.1
            session.add(
                EvaluationMeasurementRow(
                    id=identity,
                    owner_user_id=run.owner_user_id,
                    request=request.model_dump(mode="json"),
                    authorization_snapshot=run.authorization_snapshot,
                    state="succeeded",
                    version=1,
                    report=document,
                    report_sha256=report.report_sha256,
                )
            )
            await session.flush()
            profile = EvaluationCoexistenceProfileRow(
                id=uuid.uuid4(),
                measurement_id=identity,
                fingerprint_sha256="b" * 64,
                report_sha256=report.report_sha256,
                expires_at=report.valid_until,
                invalidated_at=now if fault == "invalidated" else None,
                invalidated_reason="telemetry_stale" if fault == "invalidated" else None,
            )
            session.add(profile)
            await session.commit()
        async with AsyncSession(engine, expire_on_commit=False) as recovered:
            matched = await matching_profile(recovered, "b" * 64, now=now)
            assert (matched is not None) is (fault is None)
    finally:
        await engine.dispose()
