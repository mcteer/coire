"""Batched serving proof preserves fresh registry, process, resident and owner refusals."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from evaluation_fixtures import FIXTURE, seed_evaluation, seed_evaluation_resident
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.auth import Principal
from coire_api.db import (
    Base,
    EngineProcessRow,
    EvaluationMeasurementRow,
    EvaluationRunRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelRow,
    ModelVariantRow,
    NodeRow,
    UserRow,
    VariantCopyRow,
)
from coire_api.evaluation.gateway_measurements import authorize
from coire_api.gateway.resolution import resolve_model
from coire_core.errors import EvaluationConflict, EvaluationForbidden
from coire_core.evaluation_suites.measurement import PROMPTS, prompt_digest
from coire_core.models.evaluation import (
    EvaluationMeasurementRequest,
    EvaluationProbePrepared,
    EvaluationProbePrompt,
    EvaluationResident,
    EvaluationSubject,
    EvaluationSubmission,
    EvaluationWorkload,
)
from coire_core.models.instance import InstanceState
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.registry import ModelState
from coire_scheduler.evaluation_guard import resident_engine_bindings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "change",
    [
        None,
        "foreign",
        "unready",
        "retired",
        "unvalidated",
        "copy",
        "split_manifest",
        "engine_process",
        "engine_target",
        "training",
        "owner",
        "cancelled",
        "prompt",
        "source",
    ],
)
async def test_fresh_probe_snapshot_preserves_refusals(
    training_postgres_url: str,
    change: str | None,
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as s:
            run_id = await seed_evaluation(s)
            node_id, instance_id = await seed_evaluation_resident(s)
            run = await s.get(EvaluationRunRow, run_id)
            assert run is not None
            principal = Principal.model_validate(run.authorization_snapshot)
            work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
            resident = EvaluationResident(instance_id=instance_id, target=work.target.target)
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
            identity = uuid.uuid4()
            probes = EvaluationProbePrepared(
                measurement_id=identity,
                prompt_set_sha256=prompt_digest(),
                prompts=[
                    EvaluationProbePrompt(
                        id=f"probe-{i}", text=v, tokens_by_instance={instance_id: 10}
                    )
                    for i, v in enumerate(PROMPTS)
                ],
            )
            bindings = await resident_engine_bindings(s, [resident])
            measurement = EvaluationMeasurementRow(
                id=identity,
                owner_user_id=run.owner_user_id,
                request=request.model_dump(mode="json"),
                authorization_snapshot=run.authorization_snapshot,
                execution={
                    "run_id": run_id,
                    "probes": probes.model_dump(mode="json"),
                    "resident_engines": bindings,
                },
                state="running",
                version=1,
                deadline_at=datetime.now(UTC) + timedelta(minutes=10),
            )
            s.add(measurement)
            await s.commit()
            ordinary = await resolve_model(
                s,
                resident.target.model_id,
                principal,
                variant_id=resident.target.variant_id,
                instance_id=instance_id,
            )
            await s.commit()
            if change == "foreign" or change == "training":
                s.add(
                    MemoryReservationRow(
                        id=uuid.uuid4(),
                        node_id=node_id,
                        holder_type=ReservationHolder.TRAINING
                        if change == "training"
                        else ReservationHolder.MODEL,
                        holder_id="unowned",
                        bytes=1024,
                        pinned=True,
                        state=MemoryReservationState.HELD,
                    )
                )
            elif change == "unready":
                instance = await s.get(ModelInstanceRow, instance_id)
                assert instance is not None
                instance.state = InstanceState.DRAINING
            elif change == "retired" or change == "source":
                model = await s.get(ModelRow, resident.target.model_id)
                assert model is not None
                if change == "retired":
                    model.state = ModelState.RETIRED
                else:
                    model.source = "provider"
            elif change == "unvalidated":
                variant = await s.get(ModelVariantRow, resident.target.variant_id)
                assert variant is not None
                variant.validated = False
            elif change == "copy":
                copy = await s.scalar(
                    select(VariantCopyRow).where(VariantCopyRow.node_id == node_id)
                )
                assert copy is not None
                copy.verified = False
            elif change == "split_manifest":
                other = NodeRow(
                    id=uuid.uuid4(),
                    name="coire-edge-b",
                    role="studio",
                    reachability="healthy",
                    memory_total_bytes=128 * 1024**3,
                    disk_total_bytes=1024**4,
                    gpu_cores=60,
                    agent_version="fixture",
                )
                s.add(other)
                await s.flush()
                s.add(
                    VariantCopyRow(
                        variant_id=resident.target.variant_id,
                        node_id=other.id,
                        path="/other",
                        bytes=1024,
                        manifest_sha256="c" * 64,
                        verified=True,
                        verified_at=datetime.now(UTC),
                        role="replica",
                    )
                )
            elif change in {"engine_process", "engine_target"}:
                process = await s.scalar(
                    select(EngineProcessRow).where(EngineProcessRow.instance_id == instance_id)
                )
                assert process is not None
                if change == "engine_process":
                    assert process.pid is not None
                    process.pid += 1
                else:
                    process.variant_id = None
            elif change == "owner":
                owner = await s.get(UserRow, run.owner_user_id)
                assert owner is not None
                owner.active = False
            elif change == "cancelled":
                measurement.state = "cancelled"
            await s.commit()
            prompt = probes.prompts[0]
            if change == "prompt":
                prompt = prompt.model_copy(update={"text": "foreign"})
            if change is None:
                assert await authorize(s, principal, identity, resident, prompt) == ordinary
            else:
                with pytest.raises((EvaluationConflict, EvaluationForbidden)):
                    await authorize(s, principal, identity, resident, prompt)
    finally:
        await engine.dispose()
