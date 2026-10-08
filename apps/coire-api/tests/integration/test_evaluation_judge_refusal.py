"""Self-judge refusal is audited before any durable worker or resource allocation."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from evaluation_fixtures import FIXTURE, seed_evaluation
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.auth import Principal
from coire_api.db import (
    AgentRunRow,
    AuditRow,
    Base,
    EvaluationAttemptRow,
    EvaluationRunRow,
    EvaluationSuiteRow,
    MemoryReservationRow,
)
from coire_api.evaluation.authorization import require_evaluation_principal
from coire_api.evaluation.catalog import build_suite
from coire_api.routes import admin_evaluation_runs
from coire_core.errors import CoireError
from coire_core.models.evaluation import (
    EvaluationSubject,
    EvaluationSuiteRegistration,
    EvaluationWorkload,
)
from coire_core.settings import Settings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("identity_kind", ["model", "artifact_alias", "adapter"])
async def test_self_judge_refusal_creates_only_audit(
    training_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity_kind: str
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
        candidate = work.target
        judge_identity = work.target.target.model_copy(
            update={
                "variant_id": uuid.uuid4(),
                "model_id": uuid.uuid4()
                if identity_kind == "artifact_alias"
                else candidate.target.model_id,
                "base_manifest_sha256": candidate.target.base_manifest_sha256
                if identity_kind == "artifact_alias"
                else "b" * 64,
            }
        )
        judge = candidate.model_copy(
            update={"target": judge_identity, "public_selector": judge_identity.model_id}
        )
        if identity_kind == "adapter":
            candidate = candidate.model_copy(
                update={
                    "target": candidate.target.model_copy(
                        update={
                            "adapter_id": uuid.uuid4(),
                            "adapter_manifest_sha256": "c" * 64,
                        }
                    )
                }
            )
        async with AsyncSession(engine, expire_on_commit=False) as session:
            run_id = await seed_evaluation(session)
            run = await session.get(EvaluationRunRow, run_id)
            assert run is not None
            principal = Principal.model_validate(run.authorization_snapshot)
            suite = build_suite(
                EvaluationSuiteRegistration(
                    suite_id="judge-refusal",
                    version=1,
                    template_id="judge-rubric",
                    judge=EvaluationSubject(
                        model_id=judge_identity.model_id, variant_id=judge_identity.variant_id
                    ),
                ),
                owner=run.owner_user_id,
                judge=judge,
                now=datetime.now(UTC),
            )
            session.add(
                EvaluationSuiteRow(
                    id=uuid.uuid4(),
                    suite_id=suite.suite_id,
                    version=1,
                    registry_version=1,
                    definition=suite.model_dump(mode="json"),
                    content_sha256=suite.content_sha256,
                    owner_user_id=run.owner_user_id,
                    retired=False,
                )
            )
            await session.commit()

        @asynccontextmanager
        async def scope() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session
                await session.commit()

        monkeypatch.setattr(admin_evaluation_runs, "session_scope", scope)
        resolver = AsyncMock(return_value=candidate)
        monkeypatch.setattr("coire_api.evaluation.service.resolve_evaluation_target", resolver)
        app = FastAPI()
        app.state.settings = Settings(evaluations_enabled=True, training_dataset_dir=str(tmp_path))
        app.include_router(admin_evaluation_runs.router)
        app.dependency_overrides[require_evaluation_principal] = lambda: principal

        @app.exception_handler(CoireError)
        async def problem(request: Request, error: CoireError) -> JSONResponse:
            return JSONResponse(status_code=error.status, content={"code": error.code})

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/admin/evaluations",
                headers={"Idempotency-Key": "self-refusal"},
                json={
                    "suite_id": suite.suite_id,
                    "suite_version": 1,
                    "subjects": [
                        {
                            "model_id": str(candidate.target.model_id),
                            "variant_id": str(candidate.target.variant_id),
                            "adapter_id": str(candidate.target.adapter_id)
                            if candidate.target.adapter_id
                            else None,
                        }
                    ],
                },
            )
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "evaluation_self_judge"
        async with scope() as session:
            assert await session.scalar(select(func.count()).select_from(EvaluationRunRow)) == 1
            for row_type in (AgentRunRow, EvaluationAttemptRow, MemoryReservationRow):
                assert await session.scalar(select(func.count()).select_from(row_type)) == 0
            audit = await session.scalar(
                select(AuditRow).where(AuditRow.action == "evaluation.refused")
            )
            assert audit is not None and audit.actor_user_id == principal.user_id
            assert audit.context["reason"] == "evaluation_self_judge"
    finally:
        await engine.dispose()
