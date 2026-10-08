"""Controlled mixed admission excludes only live exact owned allocating phase holds."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from evaluation_fixtures import FIXTURE, seed_evaluation, seed_evaluation_resident
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import (
    Base,
    EvaluationAttemptRow,
    EvaluationMeasurementRow,
    EvaluationRunRow,
    MemoryReservationRow,
    ModelInstanceRow,
    NodeRow,
)
from coire_core.evaluation_suites.measurement import prompt_digest
from coire_core.models.evaluation import (
    EvaluationMeasurementRequest,
    EvaluationResident,
    EvaluationSubject,
    EvaluationSubmission,
    EvaluationWorkload,
)
from coire_core.models.instance import InstanceState
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_scheduler.evaluation_guard import controlled_measurement, resident_engine_bindings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("condition", ["owned", "borrowed", "released", "wrong_node", "foreign"])
async def test_owned_allocating_hold_is_not_a_chat_resident(
    training_postgres_url: str,
    condition: str,
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as s:
            run_id = await seed_evaluation(s)
            node_id, resident_id = await seed_evaluation_resident(s)
            run = await s.get(EvaluationRunRow, run_id)
            node = await s.get(NodeRow, node_id)
            assert run is not None and node is not None
            work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
            resident = EvaluationResident(instance_id=resident_id, target=work.target.target)
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
                prompt_set_sha256=prompt_digest(),
            )
            measurement_id = uuid.uuid4()
            phase_id = uuid.uuid4()
            s.add(
                EvaluationMeasurementRow(
                    id=measurement_id,
                    owner_user_id=run.owner_user_id,
                    request=request.model_dump(mode="json"),
                    authorization_snapshot=run.authorization_snapshot,
                    state="running",
                    version=1,
                    deadline_at=datetime.now(UTC) + timedelta(minutes=10),
                    execution={
                        "run_id": run_id,
                        "phase": "mixed",
                        "suite": run.suite_snapshot,
                        "subjects": run.subjects,
                        "resident_engines": await resident_engine_bindings(s, [resident]),
                        "baseline": {
                            str(resident_id): {"count": 100, "p95": 0.2, "overhead_p95": 0.01}
                        },
                    },
                )
            )
            await s.flush()
            run.measurement_id = measurement_id
            s.add(
                ModelInstanceRow(
                    id=phase_id,
                    model_id=resident.target.model_id,
                    variant_id=resident.target.variant_id,
                    policy="single:coire-edge-a",
                    state=InstanceState.REQUESTED,
                )
            )
            await s.flush()
            s.add(
                EvaluationAttemptRow(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    phase="base",
                    ordinal=1,
                    fence=1,
                    instance_id=phase_id,
                    node_id=None if condition == "wrong_node" else node_id,
                    owns_instance=condition != "borrowed",
                    state="released" if condition == "released" else "pending",
                    target_sha256="a" * 64,
                    workload=work.model_dump(mode="json"),
                    deadline_at=datetime.now(UTC) + timedelta(minutes=10),
                )
            )
            s.add(
                MemoryReservationRow(
                    id=uuid.uuid4(),
                    node_id=node_id,
                    holder_type=ReservationHolder.MODEL,
                    holder_id=str(phase_id),
                    bytes=1024,
                    pinned=True,
                    state=MemoryReservationState.HELD,
                )
            )
            if condition == "foreign":
                s.add(
                    MemoryReservationRow(
                        id=uuid.uuid4(),
                        node_id=node_id,
                        holder_type=ReservationHolder.MODEL,
                        holder_id="unknown",
                        bytes=1024,
                        pinned=True,
                        state=MemoryReservationState.HELD,
                    )
                )
            await s.commit()
            assert await controlled_measurement(s, run, node) is (condition == "owned")
    finally:
        await engine.dispose()
