"""Real database fence and immutable terminal result behavior."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import Base, EvaluationGroupRow, EvaluationRunRow, EvaluationSuiteRow, UserRow
from coire_api.evaluation.catalog import build_suite
from coire_api.evaluation.finalizer import finalize, result_digest
from coire_core.errors import EvaluationConflict
from coire_core.models.auth import UserRole
from coire_core.models.evaluation import EvaluationSuiteRegistration

pytestmark = pytest.mark.integration
WORKLOAD_PATH = Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/workload.json"


async def test_failure_finalization_is_fenced_idempotent_and_has_no_scores(
    training_postgres_url: str,
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            owner = uuid.uuid4()
            session.add(
                UserRow(
                    id=owner,
                    email=f"{owner}@evaluation.test",
                    display_name="Admin",
                    role=UserRole.ADMIN,
                    active=True,
                )
            )
            await session.flush()
            authority = Principal(kind=PrincipalKind.USER, user_id=owner, role=UserRole.ADMIN)
            suite = build_suite(
                EvaluationSuiteRegistration(
                    suite_id="task-test", version=1, template_id="task-coding-instructions"
                ),
                owner=owner,
                judge=None,
                now=datetime.now(UTC),
            )
            catalog = EvaluationSuiteRow(
                id=uuid.uuid4(),
                suite_id=suite.suite_id,
                version=suite.version,
                definition=suite.model_dump(mode="json"),
                content_sha256=suite.content_sha256,
                owner_user_id=owner,
                attribution="admin",
                retired=False,
            )
            session.add(catalog)
            await session.flush()
            # A failed obligation retains its frozen target without requiring a live engine.

            from coire_core.models.evaluation import EvaluationWorkload

            fixture = EvaluationWorkload.model_validate_json(WORKLOAD_PATH.read_bytes())
            group_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
            session.add(
                EvaluationGroupRow(
                    id=group_id,
                    owner_user_id=owner,
                    origin="manual",
                    subjects=[fixture.target.model_dump(mode="json")],
                )
            )
            await session.flush()
            run = EvaluationRunRow(
                id=group_id,
                group_id=group_id,
                owner_user_id=owner,
                suite_row_id=catalog.id,
                suite_snapshot=suite.model_dump(mode="json"),
                subjects=[fixture.target.model_dump(mode="json")],
                authorization_snapshot=authority.model_dump(mode="json"),
                request_sha256="a" * 64,
                idempotency_key_sha256="b" * 64,
                state="running",
                phase="base",
                fence=1,
                version=1,
                next_event_sequence=1,
                cleanup_state="complete",
                evidence_reserved_bytes=0,
                queue_deadline_at=datetime.now(UTC) + timedelta(minutes=5),
                execution_deadline_at=datetime.now(UTC) + timedelta(minutes=10),
            )
            session.add(run)
            await session.commit()
            with pytest.raises(EvaluationConflict):
                await finalize(
                    session, run.id, fence=2, outcome="failed", reason="model_unavailable"
                )
            await session.rollback()
            first = await finalize(
                session, group_id, fence=1, outcome="failed", reason="model_unavailable"
            )
            await session.commit()
            second = await finalize(
                session, group_id, fence=1, outcome="failed", reason="model_unavailable"
            )
            assert first.id == second.id and first.result_sha256 == result_digest(first)
            assert first.aggregates == [None] and first.harness_verdict is None
    finally:
        await engine.dispose()
