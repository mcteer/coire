"""Failed baseline qualification retains measured summaries without minting a profile."""

import inspect
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from evaluation_fixtures import FIXTURE, seed_evaluation
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import (
    Base,
    EvaluationCoexistenceProfileRow,
    EvaluationMeasurementRow,
    EvaluationRunRow,
)
from coire_core.evaluation_suites.measurement import prompt_digest
from coire_core.models.evaluation import (
    EvaluationMeasurementRequest,
    EvaluationProbeSummary,
    EvaluationResident,
    EvaluationSubject,
    EvaluationSubmission,
    EvaluationWorkload,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("first_token,overhead", [(1.501, 0.01), (0.2, 0.0201)])
async def test_failed_baseline_retains_complete_receipt_and_no_profile(
    training_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    first_token: float,
    overhead: float,
) -> None:
    import coire_scheduler.evaluation_measurements as module

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
        identity = uuid.uuid4()
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
            prompt_set_sha256=prompt_digest(),
        )
        async with AsyncSession(engine, expire_on_commit=False) as s:
            run_id = await seed_evaluation(s)
            run = await s.get(EvaluationRunRow, run_id)
            assert run is not None
            s.add(
                EvaluationMeasurementRow(
                    id=identity,
                    owner_user_id=run.owner_user_id,
                    request=request.model_dump(mode="json"),
                    authorization_snapshot=run.authorization_snapshot,
                    state="running",
                    version=1,
                    deadline_at=datetime.now(UTC) + timedelta(minutes=10),
                    execution={
                        "phase": "claimed",
                        "run_id": run_id,
                        "suite": run.suite_snapshot,
                        "subjects": [work.target.model_dump(mode="json")],
                    },
                )
            )
            await s.commit()

        @asynccontextmanager
        async def scope() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as s, s.begin():
                yield s

        async def measured(*args: object) -> dict[str, EvaluationProbeSummary]:
            return {
                str(resident.instance_id): EvaluationProbeSummary(
                    count=100, p95=first_token, overhead_p95=overhead
                )
            }

        monkeypatch.setattr(module, "session_scope", scope)
        monkeypatch.setattr(module, "sample", measured)
        await inspect.unwrap(module.execute_measurement)(str(identity))
        async with AsyncSession(engine) as s:
            row = await s.get(EvaluationMeasurementRow, identity)
            assert row is not None
            assert row.state == "failed" and row.report and row.report["reason"] == "latency_breach"
            assert row.execution["phase"] == "baseline_complete"
            assert row.execution["baseline"] == {
                str(resident.instance_id): {
                    "count": 100,
                    "p95": first_token,
                    "overhead_p95": overhead,
                }
            }
            assert (
                await s.scalar(select(func.count()).select_from(EvaluationCoexistenceProfileRow))
                == 0
            )
    finally:
        await engine.dispose()
