"""Lost collection acknowledgments and expiry retain one immutable metadata result."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from evaluation_fixtures import FIXTURE, seed_evaluation
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import (
    Base,
    EvaluationAttemptRow,
    EvaluationEvidenceRow,
    EvaluationResultRow,
    EvaluationRunRow,
)
from coire_api.evaluation.evidence import EvidenceStore, expire, persist_collected, reserve_quota
from coire_api.evaluation.finalizer import finalize
from coire_core.errors import EvaluationConflict, EvaluationEvidenceGone, EvaluationQuotaExceeded
from coire_core.models.evaluation import (
    EvaluationSuite,
    EvaluationWorkerResult,
    EvaluationWorkload,
    canonical_digest,
)
from coire_core.settings import Settings

pytestmark = pytest.mark.integration


async def test_global_quota_refusal_preserves_existing_reservation(
    training_postgres_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(training_postgres_url)
    settings = Settings(
        training_dataset_dir=str(tmp_path), evaluation_evidence_quota_bytes=8 * 1024**2
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            identity = await seed_evaluation(session)
            run = await session.get(EvaluationRunRow, identity)
            assert run is not None
            await reserve_quota(session, run, settings)
            other = EvaluationRunRow(
                id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
                group_id=run.group_id,
                owner_user_id=run.owner_user_id,
                suite_row_id=run.suite_row_id,
                suite_snapshot=run.suite_snapshot,
                subjects=run.subjects,
                authorization_snapshot=run.authorization_snapshot,
                request_sha256="c" * 64,
                idempotency_key_sha256="d" * 64,
                state="queued",
                fence=1,
                version=1,
                queue_deadline_at=run.queue_deadline_at,
                execution_deadline_at=run.execution_deadline_at,
                cleanup_state="complete",
                evidence_reserved_bytes=0,
            )
            session.add(other)
            await session.commit()
        async with AsyncSession(engine, expire_on_commit=False) as session:
            candidate = await session.get(EvaluationRunRow, "01ARZ3NDEKTSV4RRFFQ69G5FAW")
            assert candidate is not None
            with pytest.raises(EvaluationQuotaExceeded):
                await reserve_quota(session, candidate, settings)
            await session.rollback()
            run = await session.get(EvaluationRunRow, identity)
            recovered_other = await session.get(EvaluationRunRow, "01ARZ3NDEKTSV4RRFFQ69G5FAW")
            assert run is not None and run.evidence_reserved_bytes == 8 * 1024**2
            assert recovered_other is not None and recovered_other.evidence_reserved_bytes == 0
    finally:
        await engine.dispose()


async def test_lost_ack_replays_one_private_receipt_and_expiry_keeps_terminal_digest(
    training_postgres_url: str,
    tmp_path: Path,
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
            work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes()).model_copy(
                update={
                    "evaluation_id": run_id,
                    "suite": EvaluationSuite.model_validate(run.suite_snapshot),
                }
            )
            run.state = "running"
            await reserve_quota(session, run, settings)
            session.add(
                EvaluationAttemptRow(
                    id=work.attempt_id,
                    run_id=run_id,
                    phase="base",
                    ordinal=1,
                    fence=1,
                    workload=work.model_dump(mode="json"),
                    target_sha256=canonical_digest(work.target),
                    deadline_at=work.deadline,
                    state="collecting",
                )
            )
            await session.commit()
            now = datetime.now(UTC)
            worker = EvaluationWorkerResult(
                evaluation_id=run_id,
                attempt_id=work.attempt_id,
                run_id=work.run_id,
                fence=1,
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
            evidence_id = await persist_collected(session, work.attempt_id, worker, settings)
            await session.commit()
        # A replacement collector recovers after durable commit but before acknowledgment.
        async with AsyncSession(engine, expire_on_commit=False) as session:
            assert (
                await persist_collected(session, work.attempt_id, worker, settings) == evidence_id
            )
            assert (
                await session.scalar(select(func.count()).select_from(EvaluationEvidenceRow)) == 1
            )
            with pytest.raises(EvaluationConflict):
                await persist_collected(
                    session, work.attempt_id, worker.model_copy(update={"fence": 2}), settings
                )
            await session.rollback()
            result = await finalize(
                session, run_id, fence=1, outcome="failed", reason="model_unavailable"
            )
            assert (
                await persist_collected(session, work.attempt_id, worker, settings) == evidence_id
            )
            late_work = work.model_copy(update={"attempt_id": uuid.uuid4(), "run_id": uuid.uuid4()})
            session.add(
                EvaluationAttemptRow(
                    id=late_work.attempt_id,
                    run_id=run_id,
                    phase="base",
                    ordinal=2,
                    fence=1,
                    workload=late_work.model_dump(mode="json"),
                    target_sha256=canonical_digest(work.target),
                    deadline_at=work.deadline,
                    state="collecting",
                )
            )
            await session.flush()
            late_result = worker.model_copy(
                update={
                    "attempt_id": late_work.attempt_id,
                    "run_id": late_work.run_id,
                    "request_sha256": canonical_digest(late_work),
                }
            )
            with pytest.raises(EvaluationConflict, match="no longer"):
                await persist_collected(session, late_work.attempt_id, late_result, settings)
            assert not (
                EvidenceStore(settings).root
                / str(uuid.uuid5(late_work.attempt_id, "evaluation-evidence-v1"))
            ).exists()
            row = await session.get(EvaluationEvidenceRow, evidence_id)
            assert row is not None
            row.expires_at = now - timedelta(seconds=1)
            row.pin_until = now - timedelta(seconds=1)
            await session.commit()
        async with AsyncSession(engine, expire_on_commit=False) as session:
            assert await expire(session, EvidenceStore(settings), now=now) == 1
            await session.commit()
        async with AsyncSession(engine, expire_on_commit=False) as session:
            stored = await session.scalar(
                select(EvaluationResultRow).where(EvaluationResultRow.run_id == run_id)
            )
            assert stored is not None and stored.result_sha256 == result.result_sha256
            run = await session.get(EvaluationRunRow, run_id)
            assert run is not None and run.evidence_reserved_bytes == 0
            row = await session.get(EvaluationEvidenceRow, evidence_id)
            assert row is not None and row.availability == "expired"
            assert not (EvidenceStore(settings).root / str(evidence_id)).exists()
            with pytest.raises(EvaluationEvidenceGone):
                await persist_collected(session, work.attempt_id, worker, settings)
    finally:
        await engine.dispose()
