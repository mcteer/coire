"""Actual durable HTTP intents: replay, fresh reruns, optimistic controls and audit."""

import asyncio
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

from coire_api.auth import Principal, require_principal
from coire_api.db import AuditRow, Base, EvaluationRunRow, EvaluationSuiteRow, UserRow
from coire_api.routes import admin_evaluation_runs
from coire_core.errors import CoireError
from coire_core.models.auth import UserRole
from coire_core.models.evaluation import EvaluationWorkload
from coire_core.settings import Settings
from coire_scheduler.evaluations import advance

pytestmark = pytest.mark.integration


async def test_manual_http_intent_control_replay_rerun_and_fresh_demotion(
    training_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            identity = await seed_evaluation(session)
            seed = await session.get(EvaluationRunRow, identity)
            assert seed is not None
            principal = Principal.model_validate(seed.authorization_snapshot)
        target = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes()).target

        @asynccontextmanager
        async def scope() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session
                await session.commit()

        monkeypatch.setattr(admin_evaluation_runs, "session_scope", scope)
        monkeypatch.setattr("coire_api.evaluation.authorization.session_scope", scope)
        node = AsyncMock()
        node.__aenter__.return_value = node
        monkeypatch.setattr(admin_evaluation_runs, "NodeClient", lambda settings: node)
        resolver = AsyncMock(return_value=target)
        monkeypatch.setattr("coire_api.evaluation.service.resolve_evaluation_target", resolver)
        settings = Settings(
            evaluations_enabled=True,
            training_dataset_dir=str(tmp_path),
            chat_browser_origin="https://coire.test",
        )
        app = FastAPI()
        app.state.settings = settings
        app.include_router(admin_evaluation_runs.router)
        app.dependency_overrides[require_principal] = lambda: principal

        @app.exception_handler(CoireError)
        async def problem(request: Request, error: CoireError) -> JSONResponse:
            return JSONResponse(status_code=error.status, content={"detail": str(error)})

        body = {
            "suite_id": "task-recovery",
            "suite_version": 1,
            "subjects": [
                {
                    "model_id": str(target.target.model_id),
                    "variant_id": str(target.target.variant_id),
                }
            ],
        }
        path = "/api/v1/admin/evaluations"
        headers = {"Idempotency-Key": "manual-http"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Origin": "https://coire.test"},
        ) as client:
            assert (await client.post(path, json=body)).status_code == 422
            concurrent = await asyncio.gather(
                client.post(path, json=body, headers=headers),
                client.post(path, json=body, headers=headers),
            )
            accepted = concurrent[0]
            assert concurrent[1].status_code == 202 and concurrent[1].json() == accepted.json()
            assert accepted.status_code == 202, accepted.text
            receipt = accepted.json()
            assert (await client.post(path, json=body, headers=headers)).json() == receipt
            assert resolver.await_count == 1
            assert (
                await client.post(
                    path, json={**body, "expected_engine_version": "changed"}, headers=headers
                )
            ).status_code == 409
            run_path = f"{path}/{receipt['id']}"
            assert (await client.get(run_path)).json()["owner_user_id"] == str(principal.user_id)
            page = await client.get(path, params={"limit": 1})
            assert page.status_code == 200 and page.json()["next_cursor"]
            older = await client.get(
                path, params={"limit": 1, "cursor": page.json()["next_cursor"]}
            )
            assert older.status_code == 200 and older.json()["items"][0]["id"] == identity
            assert (
                await client.get(
                    path, params={"state": "failed", "cursor": page.json()["next_cursor"]}
                )
            ).status_code == 422
            assert (await client.get(f"{run_path}/events", params={"after": -1})).status_code == 422
            cancel_headers = {"Idempotency-Key": "cancel-http"}
            assert (
                await client.post(
                    f"{run_path}/cancel", json={"expected_version": 99}, headers=cancel_headers
                )
            ).status_code == 409
            cancelled = await client.post(
                f"{run_path}/cancel",
                json={"expected_version": receipt["version"]},
                headers=cancel_headers,
            )
            assert cancelled.status_code == 202 and cancelled.json()["state"] == "cancelling"
            assert (
                await client.post(
                    f"{run_path}/cancel",
                    json={"expected_version": receipt["version"]},
                    headers=cancel_headers,
                )
            ).json() == cancelled.json()
            async with scope() as session:
                assert await advance(session, receipt["id"], settings)
            original = (await client.get(run_path)).json()
            assert original["state"] == "cancelled" and original["result"]["aggregates"] == [None]
            rerun_headers = {"Idempotency-Key": "rerun-http"}
            rerun_body = {"expected_version": original["version"]}
            rerun = await client.post(f"{run_path}/rerun", json=rerun_body, headers=rerun_headers)
            assert rerun.status_code == 202, rerun.text
            assert (
                rerun.json()["id"] != receipt["id"]
                and rerun.json()["group_id"] != receipt["group_id"]
            )
            assert (
                await client.post(f"{run_path}/rerun", json=rerun_body, headers=rerun_headers)
            ).json() == rerun.json()
            assert (await client.get(run_path)).json()["result"]["result_sha256"] == original[
                "result"
            ]["result_sha256"]
            async with scope() as session:
                catalog = (await session.scalars(select(EvaluationSuiteRow))).one()
                catalog.retired = True
                catalog.registry_version += 1
            retired = (await client.get(run_path)).json()
            assert retired["suite"]["retired"]
            assert retired["result"]["result_sha256"] == original["result"]["result_sha256"]
            assert (
                await client.post(
                    f"{run_path}/rerun",
                    json=rerun_body,
                    headers={"Idempotency-Key": "new-after-retirement"},
                )
            ).status_code == 409
            assert (
                await client.post(f"{run_path}/rerun", json=rerun_body, headers=rerun_headers)
            ).json() == rerun.json()
            async with scope() as session:
                user = await session.get(UserRow, principal.user_id)
                assert user is not None
                user.role = UserRole.USER
            assert (
                await client.post(f"{run_path}/rerun", json=rerun_body, headers=rerun_headers)
            ).status_code == 403
            assert (await client.post(path, json=body, headers=headers)).status_code == 403
            for read_path in (
                run_path,
                path,
                f"{run_path}/events",
                f"/api/v1/admin/evaluation-groups/{receipt['group_id']}",
            ):
                assert (await client.get(read_path)).status_code == 403
        async with scope() as session:
            runs = (await session.scalars(select(EvaluationRunRow))).all()
            assert len(runs) == 3 and all(run.owner_user_id == principal.user_id for run in runs)
            new = next(run for run in runs if run.id == rerun.json()["id"])
            assert new.source_run_id == receipt["id"]
            audits = (await session.scalars(select(AuditRow))).all()
            assert any(row.action == "evaluation.refused" for row in audits)
            assert any(row.action == "evaluation.cancel" for row in audits)
    finally:
        await engine.dispose()
