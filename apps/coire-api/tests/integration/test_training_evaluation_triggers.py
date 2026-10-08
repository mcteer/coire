"""Final success and declared evaluation obligations form one durable transaction."""

import pytest
from evaluation_training_fixtures import seed_evaluated_training
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import Base, TrainingAdapterRow, TrainingEvaluationTriggerRow, TrainingJobRow
from coire_core.settings import Settings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "version,state,expected",
    [
        (1, "succeeded", False),
        (2, "succeeded", True),
        (2, "failed", False),
        (2, "cancelled", False),
    ],
)
async def test_final_obligation_opt_in_replay_and_queue_independence(
    training_postgres_url: str, version: int, state: str, expected: bool
) -> None:
    from coire_api.evaluation.training import ensure_final_trigger

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job_id, adapter_id = await seed_evaluated_training(
                session, version=version, state=state
            )
        first = None
        for _ in range(2):
            async with AsyncSession(engine, expire_on_commit=False) as recovered:
                job = await recovered.get(TrainingJobRow, job_id, with_for_update=True)
                adapter = await recovered.get(TrainingAdapterRow, adapter_id)
                assert job is not None and adapter is not None
                trigger = await ensure_final_trigger(
                    recovered,
                    job,
                    adapter,
                    settings=Settings(evaluations_enabled=False, evaluation_max_pending_runs=1),
                )
                await recovered.commit()
                assert (trigger is not None) is expected
                assert job.state == state and adapter.state == "ready" and not adapter.verified
                if trigger is not None:
                    assert (
                        trigger.boundary_kind == "final"
                        and trigger.completed_update == job.completed_update
                    )
                    assert len(trigger.schedules) == 2 and trigger.group_id is None
                    assert trigger.id == first if first is not None else True
                    first = trigger.id
                assert await recovered.scalar(
                    select(func.count()).select_from(TrainingEvaluationTriggerRow)
                ) == int(expected)
    finally:
        await engine.dispose()


async def test_uncommitted_training_success_leaves_no_final_obligation_on_rollback(
    training_postgres_url: str,
) -> None:
    from coire_api.evaluation.training import ensure_final_trigger

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job_id, adapter_id = await seed_evaluated_training(session, state="finalizing")
            job = await session.get(TrainingJobRow, job_id, with_for_update=True)
            adapter = await session.get(TrainingAdapterRow, adapter_id)
            assert job is not None and adapter is not None
            job.state = "succeeded"
            assert (
                await ensure_final_trigger(session, job, adapter, settings=Settings()) is not None
            )
            await session.rollback()
        async with AsyncSession(engine, expire_on_commit=False) as recovered:
            job = await recovered.get(TrainingJobRow, job_id)
            assert job is not None and job.state == "finalizing"
            assert (
                await recovered.scalar(
                    select(func.count()).select_from(TrainingEvaluationTriggerRow)
                )
                == 0
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize("blocked_by", ["disabled", "full", None])
async def test_final_reconciliation_keeps_obligation_until_admission_or_explicit_expiry(
    training_postgres_url: str, blocked_by: str | None
) -> None:
    from datetime import UTC, datetime, timedelta

    from coire_api.db import EvaluationGroupRow, EvaluationResultRow, EvaluationRunRow
    from coire_api.evaluation.training import ensure_final_trigger, reconcile_final_trigger

    settings = Settings(
        evaluations_enabled=blocked_by != "disabled",
        evaluation_max_pending_runs=1 if blocked_by == "full" else 100,
    )
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job_id, adapter_id = await seed_evaluated_training(session)
            job = await session.get(TrainingJobRow, job_id, with_for_update=True)
            adapter = await session.get(TrainingAdapterRow, adapter_id)
            assert job is not None and adapter is not None
            trigger = await ensure_final_trigger(session, job, adapter, settings=settings)
            assert trigger is not None
            trigger_id = trigger.id
            await session.commit()
        async with AsyncSession(engine, expire_on_commit=False) as recovered:
            assert not await reconcile_final_trigger(recovered, trigger_id, settings=settings)
            await recovered.commit()
            trigger = await recovered.get(TrainingEvaluationTriggerRow, trigger_id)
            assert trigger is not None
            if blocked_by is not None:
                assert trigger.group_id is None
                assert (
                    await recovered.scalar(select(func.count()).select_from(EvaluationRunRow)) == 1
                )
                trigger.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
                await recovered.commit()
            else:
                assert trigger.group_id is not None
        async with AsyncSession(engine, expire_on_commit=False) as recovered:
            assert await reconcile_final_trigger(recovered, trigger_id, settings=settings) is (
                blocked_by is not None
            )
            await recovered.commit()
            trigger = await recovered.get(TrainingEvaluationTriggerRow, trigger_id)
            assert trigger is not None and trigger.group_id is not None
            group = await recovered.get(EvaluationGroupRow, trigger.group_id)
            assert group is not None and group.origin == "training_final" and group.job_id == job_id
            rows = list(
                (
                    await recovered.scalars(
                        select(EvaluationRunRow).where(EvaluationRunRow.group_id == group.id)
                    )
                ).all()
            )
            assert len(rows) == 2
            assert all(
                row.data_snapshot and row.data_snapshot["completed_update"] == 32 for row in rows
            )
            results = list(
                (
                    await recovered.scalars(
                        select(EvaluationResultRow).where(
                            EvaluationResultRow.run_id.in_([row.id for row in rows])
                        )
                    )
                ).all()
            )
            if blocked_by is not None:
                assert len(results) == 2 and all(
                    result.result["aggregates"] == [None, None] for result in results
                )
                assert all(row.evidence_reserved_bytes == 0 for row in rows)
                for result in results:
                    contamination = result.result["contamination"]
                    assert (
                        isinstance(contamination, dict) and contamination["status"] == "unavailable"
                    )
                assert {result.result["reason"] for result in results} == {
                    "admission_disabled" if blocked_by == "disabled" else "capacity_timeout"
                }
            else:
                assert results == [] and all(
                    row.state == "queued" and row.evidence_reserved_bytes == 8 * 1024**2
                    for row in rows
                )
            job = await recovered.get(TrainingJobRow, job_id)
            adapter = await recovered.get(TrainingAdapterRow, adapter_id)
            assert job is not None and job.state == "succeeded"
            assert adapter is not None and adapter.state == "ready" and not adapter.verified
    finally:
        await engine.dispose()


@pytest.mark.parametrize("condition", ["valid", "legacy", "foreign_subject", "unfinished"])
async def test_manual_training_context_requires_exact_completed_lineage(
    training_postgres_url: str,
    condition: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    from evaluation_fixtures import FIXTURE

    from coire_api.evaluation.inputs import manual_training_context
    from coire_core.errors import EvaluationValidationError
    from coire_core.models.evaluation import EvaluationWorkload

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job_id, _adapter_id = await seed_evaluated_training(
                session,
                version=1 if condition == "legacy" else 2,
                state="running" if condition == "unfinished" else "succeeded",
            )
            target = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes()).target
            if condition == "foreign_subject":
                target = target.model_copy(
                    update={"target": target.target.model_copy(update={"model_id": uuid.uuid4()})}
                )
            if condition in {"valid", "legacy"}:
                snapshot, checkpoint = await manual_training_context(session, job_id, [target])
                job = await session.get(TrainingJobRow, job_id)
                assert job is not None
                assert snapshot["resolved_sha256"] == job.resolved_sha256
                assert snapshot["completed_update"] == job.completed_update
                assert snapshot["checkpoint_id"] == str(checkpoint.id)
                from unittest.mock import AsyncMock

                from coire_api.auth import Principal
                from coire_api.db import EvaluationGroupRow, EvaluationRunRow
                from coire_api.evaluation.service import submit
                from coire_core.models.evaluation import EvaluationSubject, EvaluationSubmission

                monkeypatch.setattr(
                    "coire_api.evaluation.service.resolve_evaluation_target",
                    AsyncMock(return_value=target),
                )
                receipt = await submit(
                    session,
                    Principal.model_validate(job.authorization_snapshot),
                    EvaluationSubmission(
                        suite_id="task-recovery",
                        suite_version=1,
                        training_job_id=job_id,
                        subjects=[
                            EvaluationSubject(
                                model_id=target.target.model_id, variant_id=target.target.variant_id
                            )
                        ],
                    ),
                    idempotency_key="manual-input-context",
                    settings=Settings(evaluations_enabled=True),
                    client=AsyncMock(),
                )
                stored = await session.get(EvaluationRunRow, receipt.id)
                group = await session.get(EvaluationGroupRow, receipt.group_id)
                assert stored is not None and stored.data_snapshot == snapshot
                assert (
                    group is not None
                    and group.job_id == job_id
                    and group.checkpoint_id == checkpoint.id
                )

            else:
                with pytest.raises(EvaluationValidationError):
                    await manual_training_context(session, job_id, [target])
    finally:
        await engine.dispose()
