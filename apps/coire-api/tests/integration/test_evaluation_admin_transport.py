"""Real transaction-backed HTTP replay, cancellation and historical reads."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from evaluation_fixtures import FIXTURE, seed_evaluation
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
from coire_api.routes import admin_evaluation_runs
from coire_core.errors import CoireError
from coire_core.models.evaluation import EvaluationSubject, EvaluationSubmission, EvaluationWorkload
from coire_core.settings import Settings
from coire_scheduler.evaluations import advance

pytestmark = pytest.mark.integration


async def test_http_submission_replay_versioned_stop_and_history_when_disabled(
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
            original = await seed_evaluation(session)
            row = await session.get(EvaluationRunRow, original)
            assert row is not None
            principal = Principal.model_validate(row.authorization_snapshot)

        @asynccontextmanager
        async def scope() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session
                await session.commit()

        monkeypatch.setattr(admin_evaluation_runs, "session_scope", scope)
        work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
        resolver = AsyncMock(return_value=work.target)
        monkeypatch.setattr("coire_api.evaluation.service.resolve_evaluation_target", resolver)
        app = FastAPI()
        app.state.settings = Settings(evaluations_enabled=True, training_dataset_dir=str(tmp_path))
        app.include_router(admin_evaluation_runs.router)
        app.dependency_overrides[require_evaluation_principal] = lambda: principal
        app.dependency_overrides[require_evaluation_reader] = lambda: principal

        @app.exception_handler(CoireError)
        async def problem(request: Request, error: CoireError) -> JSONResponse:
            return JSONResponse(status_code=error.status, content={"detail": str(error)})

        body = EvaluationSubmission(
            suite_id="task-recovery",
            suite_version=1,
            subjects=[
                EvaluationSubject(
                    model_id=work.target.target.model_id,
                    variant_id=work.target.target.variant_id,
                )
            ],
        ).model_dump(mode="json")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/v1/admin/evaluations", json=body)
            assert response.status_code == 422
            headers = {"Idempotency-Key": "synthetic-submit"}
            response = await client.post("/api/v1/admin/evaluations", json=body, headers=headers)
            assert response.status_code == 202
            receipt = response.json()
            run_id = receipt["id"]
            assert run_id != original
            app.state.settings.evaluations_enabled = False
            replay = await client.post("/api/v1/admin/evaluations", json=body, headers=headers)
            assert replay.status_code == 202 and replay.json() == receipt
            changed = await client.post(
                "/api/v1/admin/evaluations",
                json={**body, "expected_engine_version": "changed"},
                headers=headers,
            )
            assert changed.status_code == 409
            fresh = await client.post(
                "/api/v1/admin/evaluations", json=body, headers={"Idempotency-Key": "fresh"}
            )
            assert fresh.status_code == 503
            stop_path = f"/api/v1/admin/evaluations/{run_id}/cancel"
            stale = await client.post(
                stop_path, json={"expected_version": 8}, headers={"Idempotency-Key": "stale"}
            )
            assert stale.status_code == 409
            stop_headers = {"Idempotency-Key": "stop"}
            stop = await client.post(stop_path, json={"expected_version": 1}, headers=stop_headers)
            assert stop.status_code == 202 and stop.json()["state"] == "cancelling"
            replay = await client.post(
                stop_path, json={"expected_version": 1}, headers=stop_headers
            )
            assert replay.status_code == 202 and replay.json() == stop.json()
            async with scope() as session:
                assert await advance(session, run_id, app.state.settings)
            detail = await client.get(f"/api/v1/admin/evaluations/{run_id}")
            assert detail.status_code == 200 and detail.json()["result"]["aggregates"] == [None]
            result_digest = detail.json()["result"]["result_sha256"]
            page = await client.get(
                "/api/v1/admin/evaluations",
                params={"model_id": str(work.target.target.model_id), "limit": 1},
            )
            assert page.status_code == 200 and len(page.json()["items"]) == 1
            assert page.json()["next_cursor"]
            next_page = await client.get(
                "/api/v1/admin/evaluations",
                params={
                    "model_id": str(work.target.target.model_id),
                    "limit": 1,
                    "cursor": page.json()["next_cursor"],
                },
            )
            assert next_page.status_code == 200 and len(next_page.json()["items"]) == 1
            foreign_cursor = await client.get(
                "/api/v1/admin/evaluations",
                params={"state": "queued", "cursor": page.json()["next_cursor"]},
            )
            assert foreign_cursor.status_code == 422
            event_page = await client.get(f"/api/v1/admin/evaluations/{run_id}/events")
            assert (
                event_page.status_code == 200
                and event_page.json()["events"][-1]["kind"] == "terminal"
            )
            group_path = f"/api/v1/admin/evaluation-groups/{receipt['group_id']}"
            group_detail = await client.get(group_path)
            assert group_detail.status_code == 200 and group_detail.json()["state"] == "cancelled"
            group_page = await client.get(f"{group_path}/events")
            assert (
                group_page.status_code == 200
                and group_page.json()["events"][-1]["kind"] == "terminal"
            )
            group_stream = await client.get(
                f"{group_path}/events", headers={"Accept": "text/event-stream"}
            )
            assert group_stream.status_code == 200 and "event: terminal" in group_stream.text
            rerun_path = f"/api/v1/admin/evaluations/{run_id}/rerun"
            app.state.settings.evaluations_enabled = True
            rerun_response = await client.post(
                rerun_path,
                json={"expected_version": detail.json()["version"]},
                headers={"Idempotency-Key": "rerun"},
            )
            assert rerun_response.status_code == 202 and rerun_response.json()["id"] != run_id
            assert (await client.get(f"/api/v1/admin/evaluations/{run_id}")).json()["result"][
                "result_sha256"
            ] == result_digest
        async with scope() as session:
            refused = (
                await session.scalars(
                    select(AuditRow).where(AuditRow.action == "evaluation.refused")
                )
            ).all()
            assert len(refused) == 3
            assert {row.context["reason"] for row in refused} == {
                "evaluation_conflict",
                "evaluation_admission_disabled",
            }
            assert all(row.actor_user_id == principal.user_id for row in refused)
            assert (
                await session.scalar(
                    select(AuditRow.id).where(AuditRow.action == "evaluation.rerun")
                )
                is not None
            )
    finally:
        await engine.dispose()
