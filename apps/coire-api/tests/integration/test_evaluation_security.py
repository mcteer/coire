"""Collector provenance cannot inject a different owner/target/fence into history."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from evaluation_fixtures import FIXTURE, seed_evaluation
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import Base, EvaluationAttemptRow, EvaluationEvidenceRow, EvaluationRunRow
from coire_api.evaluation.evidence import persist_collected, reserve_quota
from coire_core.errors import EvaluationConflict, EvaluationValidationError
from coire_core.models.evaluation import (
    EvaluationWorkerResult,
    EvaluationWorkload,
    canonical_digest,
)
from coire_core.settings import Settings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("forgery", ["attempt", "fence", "target", "runtime", "request"])
async def test_foreign_collected_evidence_is_refused_before_private_storage(
    training_postgres_url: str,
    tmp_path: Path,
    forgery: str,
) -> None:
    engine = create_async_engine(training_postgres_url)
    settings = Settings(training_dataset_dir=str(tmp_path), evaluations_enabled=True)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            run_id = await seed_evaluation(session)
            run = await session.get(EvaluationRunRow, run_id)
            assert run is not None
            run.state = "running"
            await reserve_quota(session, run, settings)
            work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
            session.add(
                EvaluationAttemptRow(
                    id=work.attempt_id,
                    run_id=run_id,
                    phase=work.phase,
                    ordinal=1,
                    fence=work.fence,
                    workload=work.model_dump(mode="json"),
                    target_sha256=canonical_digest(work.target),
                    deadline_at=work.deadline,
                    state="collecting",
                )
            )
            await session.commit()
            now = datetime.now(UTC)
            result = EvaluationWorkerResult(
                evaluation_id=run_id,
                attempt_id=work.attempt_id,
                run_id=work.run_id,
                fence=work.fence,
                phase=work.phase,
                request_sha256=canonical_digest(work),
                suite_sha256=work.suite.content_sha256,
                cases_sha256=work.suite.template.cases_sha256,
                runtime=work.target.runtime,
                target=work.target.target,
                outcome="failed",
                reason="model_unavailable",
                started_at=now,
                finished_at=now,
            )
            changes: dict[str, dict[str, object]] = {
                "attempt": {"attempt_id": uuid.uuid4()},
                "fence": {"fence": 2},
                "target": {"target": result.target.model_copy(update={"variant_id": uuid.uuid4()})},
                "runtime": {
                    "runtime": result.runtime.model_copy(update={"engine_version": "foreign"})
                },
                "request": {"request_sha256": "f" * 64},
            }
            with pytest.raises((EvaluationConflict, EvaluationValidationError)):
                await persist_collected(
                    session, work.attempt_id, result.model_copy(update=changes[forgery]), settings
                )
            await session.rollback()
        async with AsyncSession(engine, expire_on_commit=False) as session:
            assert (
                await session.scalar(select(func.count()).select_from(EvaluationEvidenceRow)) == 0
            )
            attempt = await session.get(EvaluationAttemptRow, work.attempt_id)
            assert attempt is not None and attempt.state == "collecting"
            assert attempt.collected_sha256 is None
            assert not (tmp_path / "evaluation-evidence").exists()
    finally:
        await engine.dispose()
