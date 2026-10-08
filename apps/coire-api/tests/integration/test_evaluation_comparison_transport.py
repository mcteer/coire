"""Read-only result comparison retains failed/legacy provenance and live admin scope."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from evaluation_fixtures import FIXTURE, seed_evaluation, seed_evaluation_resident
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.auth import Principal
from coire_api.db import Base, EvaluationRunRow, HarnessEvaluationRow, UserRow
from coire_api.evaluation.authorization import require_evaluation_reader
from coire_api.evaluation.finalizer import finalize
from coire_api.routes import admin_evaluation_runs
from coire_core.errors import CoireError
from coire_core.models.auth import UserRole
from coire_core.models.evaluation import EvaluationWorkload
from coire_core.models.harness import EvaluationVerdict
from coire_core.settings import Settings

pytestmark = pytest.mark.integration


async def test_comparison_http_failed_legacy_unknown_and_revoked_reads(
    training_postgres_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            identity = await seed_evaluation(session)
            await seed_evaluation_resident(session)
            row = await session.get(EvaluationRunRow, identity)
            assert row is not None
            principal = Principal.model_validate(row.authorization_snapshot)
            failed = await finalize(session, identity, fence=1, outcome="failed", reason="internal")
            work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
            legacy_id = uuid.uuid4()
            session.add(
                HarnessEvaluationRow(
                    id=legacy_id,
                    variant_id=work.target.target.variant_id,
                    scores={
                        "tool_calling": 1.0,
                        "structured_output": 1.0,
                        "edit_application": 1.0,
                        "long_context": 1.0,
                    },
                    overall_score=1.0,
                    verdict=EvaluationVerdict.PASSED,
                    harness_version="legacy",
                    engine_version="legacy",
                    diagnostics=[],
                )
            )
            await session.commit()

        @asynccontextmanager
        async def scope() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session
                await session.commit()

        monkeypatch.setattr(admin_evaluation_runs, "session_scope", scope)
        app = FastAPI()
        app.state.settings = Settings(evaluations_enabled=False, training_dataset_dir=str(tmp_path))
        app.include_router(admin_evaluation_runs.router)
        app.dependency_overrides[require_evaluation_reader] = lambda: principal

        @app.exception_handler(CoireError)
        async def problem(request: Request, error: CoireError) -> JSONResponse:
            return JSONResponse(status_code=error.status, content={"detail": str(error)})

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            query: dict[str, str | int] = {
                "left_result_id": failed.id,
                "right_result_id": failed.id,
                "left_subject": 0,
                "right_subject": 0,
            }
            response = await client.get("/api/v1/admin/evaluation-comparisons", params=query)
            assert response.status_code == 200
            assert response.json()["reasons"] == ["outcome"] and response.json()["delta"] is None
            legacy = await client.get(
                "/api/v1/admin/evaluation-comparisons",
                params={**query, "left_result_id": str(legacy_id)},
            )
            assert legacy.status_code == 200 and legacy.json()["reasons"] == ["legacy_provenance"]
            invalid = await client.get(
                "/api/v1/admin/evaluation-comparisons", params={**query, "left_subject": -1}
            )
            assert invalid.status_code == 422
            missing = await client.get(
                "/api/v1/admin/evaluation-comparisons",
                params={**query, "left_result_id": str(uuid.uuid4())},
            )
            assert missing.status_code == 404
            async with AsyncSession(engine) as session:
                user = await session.get(UserRow, principal.user_id)
                assert user is not None
                user.role = UserRole.USER
                await session.commit()
            revoked = await client.get("/api/v1/admin/evaluation-comparisons", params=query)
            assert revoked.status_code == 403
    finally:
        await engine.dispose()
