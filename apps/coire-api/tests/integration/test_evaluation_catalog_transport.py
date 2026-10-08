"""Immutable catalog mutations use durable receipts and retirement-safe history."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from evaluation_fixtures import seed_evaluation
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.auth import Principal
from coire_api.db import AuditRow, Base, EvaluationRunRow
from coire_api.evaluation.authorization import (
    require_evaluation_principal,
    require_evaluation_reader,
)
from coire_api.routes import admin_evaluation_suites
from coire_core.errors import CoireError
from coire_core.evaluation_suites import templates
from coire_core.settings import Settings

pytestmark = pytest.mark.integration


async def test_catalog_replay_immutable_version_and_optimistic_retirement(
    training_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
            principal = Principal.model_validate(run.authorization_snapshot)

        @asynccontextmanager
        async def scope() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session
                await session.commit()

        monkeypatch.setattr(admin_evaluation_suites, "session_scope", scope)
        app = FastAPI()
        app.state.settings = Settings(training_dataset_dir=str(tmp_path))
        app.include_router(admin_evaluation_suites.router)
        app.dependency_overrides[require_evaluation_principal] = lambda: principal
        app.dependency_overrides[require_evaluation_reader] = lambda: principal

        @app.exception_handler(CoireError)
        async def problem(request: Request, error: CoireError) -> JSONResponse:
            return JSONResponse(status_code=error.status, content={"detail": str(error)})

        task = next(item for item in templates() if item.kind.value == "task")
        body = {"suite_id": "task-http", "version": 1, "template_id": task.template_id}
        path = "/api/v1/admin/evaluation-suites"
        headers = {"Idempotency-Key": "catalog-register"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            installed = await client.get("/api/v1/admin/evaluation-suite-templates")
            assert installed.status_code == 200 and installed.json()["items"]
            assert (await client.post(path, json=body)).status_code == 422
            response = await client.post(path, json=body, headers=headers)
            assert response.status_code == 201, response.text
            receipt = response.json()
            assert (await client.post(path, json=body, headers=headers)).json() == receipt
            changed = {**body, "timeout_seconds": 120}
            assert (await client.post(path, json=changed, headers=headers)).status_code == 409
            assert (
                await client.post(path, json=changed, headers={"Idempotency-Key": "other"})
            ).status_code == 409
            version_path = f"{path}/task-http/versions/1"
            assert (await client.get(version_path)).json() == receipt
            assert (
                await client.post(
                    f"{version_path}/retire",
                    json={"expected_version": 2},
                    headers={"Idempotency-Key": "stale"},
                )
            ).status_code == 409
            retire_headers = {"Idempotency-Key": "retire"}
            retired = await client.post(
                f"{version_path}/retire", json={"expected_version": 1}, headers=retire_headers
            )
            assert retired.status_code == 200
            assert retired.json()["retired"] and retired.json()["registry_version"] == 2
            assert retired.json()["content_sha256"] == receipt["content_sha256"]
            replay = await client.post(
                f"{version_path}/retire", json={"expected_version": 1}, headers=retire_headers
            )
            assert replay.json() == retired.json()
            assert (await client.get(version_path)).json() == retired.json()
            page = await client.get(path, params={"retired": True, "limit": 1})
            assert page.status_code == 200 and page.json()["items"] == [retired.json()]
            assert (await client.get(path, params={"limit": 101})).status_code == 422
        async with scope() as session:
            audits = list((await session.scalars(select(AuditRow))).all())
            assert (
                sum(
                    row.action == "evaluation.suite.register" and row.outcome.value == "ok"
                    for row in audits
                )
                == 1
            )
            assert (
                sum(
                    row.action == "evaluation.suite.retire" and row.outcome.value == "ok"
                    for row in audits
                )
                == 1
            )
            assert sum(row.outcome.value == "refused" for row in audits) == 3
    finally:
        await engine.dispose()
