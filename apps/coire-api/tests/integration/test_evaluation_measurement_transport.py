"""Measurement HTTP history/control remain usable when admission is disabled."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from evaluation_fixtures import FIXTURE, seed_evaluation
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.auth import Principal, require_admin
from coire_api.db import Base, EvaluationMeasurementRow, EvaluationRunRow, UserRow
from coire_api.evaluation import authorization
from coire_api.routes import admin_evaluation_runs
from coire_core.errors import CoireError
from coire_core.evaluation_suites.measurement import prompt_digest
from coire_core.models.auth import UserRole
from coire_core.models.evaluation import (
    EvaluationMeasurementRequest,
    EvaluationResident,
    EvaluationSubject,
    EvaluationSubmission,
    EvaluationWorkload,
)
from coire_core.settings import Settings

pytestmark = pytest.mark.integration


async def test_measurement_control_history_refusal_and_live_owner_reads(
    training_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
        request = EvaluationMeasurementRequest(
            evaluation=EvaluationSubmission(
                suite_id="task-recovery",
                suite_version=1,
                subjects=[
                    EvaluationSubject(
                        model_id=work.target.target.model_id,
                        variant_id=work.target.target.variant_id,
                    )
                ],
            ),
            node="coire-edge-a",
            resident_targets=[
                EvaluationResident(instance_id=uuid.uuid4(), target=work.target.target)
            ],
            prompt_set_sha256=prompt_digest(),
        )
        identity = uuid.uuid4()
        async with AsyncSession(engine, expire_on_commit=False) as session:
            run_id = await seed_evaluation(session)
            run = await session.get(EvaluationRunRow, run_id)
            assert run is not None
            principal = Principal.model_validate(run.authorization_snapshot)
            session.add(
                EvaluationMeasurementRow(
                    id=identity,
                    owner_user_id=run.owner_user_id,
                    request=request.model_dump(mode="json"),
                    authorization_snapshot=run.authorization_snapshot,
                    execution={"run_id": run_id},
                    version=1,
                    state="queued",
                    deadline_at=datetime.now(UTC) + timedelta(minutes=10),
                )
            )
            await session.commit()

        @asynccontextmanager
        async def scope() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session
                await session.commit()

        monkeypatch.setattr(admin_evaluation_runs, "session_scope", scope)
        monkeypatch.setattr(authorization, "session_scope", scope)
        app = FastAPI()
        app.state.settings = Settings(evaluations_enabled=False, training_dataset_dir=str(tmp_path))
        app.include_router(admin_evaluation_runs.router)
        app.dependency_overrides[authorization.require_evaluation_principal] = lambda: principal
        app.dependency_overrides[require_admin] = lambda: principal

        @app.exception_handler(CoireError)
        async def problem(request: Request, error: CoireError) -> JSONResponse:
            return JSONResponse(status_code=error.status, content={"code": error.code})

        path = f"/api/v1/admin/evaluation-measurements/{identity}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            detail = await client.get(path)
            assert detail.status_code == 200, detail.text
            assert detail.json()["profile_status"] == "unqualified"
            missing = await client.get(f"/api/v1/admin/evaluation-measurements/{uuid.uuid4()}")
            assert missing.status_code == 404
            body = request.model_dump(mode="json")
            assert (
                await client.post("/api/v1/admin/evaluation-measurements", json=body)
            ).status_code == 422
            bad = {**body, "prompt_set_sha256": "f" * 64}
            refusal = await client.post(
                "/api/v1/admin/evaluation-measurements",
                json=bad,
                headers={"Idempotency-Key": "bad-probes"},
            )
            assert refusal.status_code == 422
            assert (
                await client.post(
                    f"{path}/cancel",
                    json={"expected_version": 2},
                    headers={"Idempotency-Key": "stale"},
                )
            ).status_code == 409
            headers = {"Idempotency-Key": "measurement-stop"}
            stopped = await client.post(
                f"{path}/cancel", json={"expected_version": 1}, headers=headers
            )
            assert stopped.status_code == 202, stopped.text
            assert stopped.json()["state"] == "cancelled" and stopped.json()["version"] == 2
            assert (
                await client.post(f"{path}/cancel", json={"expected_version": 1}, headers=headers)
            ).json() == stopped.json()
            assert (await client.get(path)).json()["state"] == "cancelled"
            async with scope() as session:
                owner = await session.get(UserRow, principal.user_id)
                assert owner is not None
                owner.role = UserRole.USER
            assert (await client.get(path)).status_code == 403
    finally:
        await engine.dispose()


async def test_measurement_submission_atomically_binds_run_and_protective_leases(
    training_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from evaluation_fixtures import seed_evaluation_resident
    from sqlalchemy import func, select

    from coire_api.db import EvaluationGroupRow, EvaluationMeasurementRow
    from coire_core.evaluation_suites.measurement import PROMPTS
    from coire_core.models.evaluation import (
        EvaluationProbePrepare,
        EvaluationProbePrepared,
        EvaluationProbePrompt,
    )

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            run_id = await seed_evaluation(session)
            run = await session.get(EvaluationRunRow, run_id)
            assert run is not None
            principal = Principal.model_validate(run.authorization_snapshot)
            _, instance_id = await seed_evaluation_resident(session)

        @asynccontextmanager
        async def scope() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session
                await session.commit()

        work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
        target = work.target.model_copy(update={"variant_slug": "synthetic-evaluation"})
        resolver = AsyncMock(return_value=target)
        monkeypatch.setattr("coire_api.evaluation.measurements.resolve_evaluation_target", resolver)
        monkeypatch.setattr("coire_api.evaluation.service.resolve_evaluation_target", resolver)
        client = AsyncMock()
        client.__aenter__.return_value = client

        async def prepare(node: str, command: EvaluationProbePrepare) -> EvaluationProbePrepared:
            assert node == "coire-edge-a"
            return EvaluationProbePrepared(
                measurement_id=command.measurement_id,
                prompt_set_sha256=command.prompt_set_sha256,
                prompts=[
                    EvaluationProbePrompt(
                        id=f"probe-{index}", text=text, tokens_by_instance={instance_id: 10}
                    )
                    for index, text in enumerate(PROMPTS)
                ],
            )

        client.prepare_evaluation_probes.side_effect = prepare
        monkeypatch.setattr(admin_evaluation_runs, "session_scope", scope)
        monkeypatch.setattr(admin_evaluation_runs, "NodeClient", lambda settings: client)
        app = FastAPI()
        app.state.settings = Settings(evaluations_enabled=True, training_dataset_dir=str(tmp_path))
        app.include_router(admin_evaluation_runs.router)
        app.dependency_overrides[authorization.require_evaluation_principal] = lambda: principal

        @app.exception_handler(CoireError)
        async def problem(request: Request, error: CoireError) -> JSONResponse:
            return JSONResponse(status_code=error.status, content={"code": error.code})

        body = EvaluationMeasurementRequest(
            evaluation=EvaluationSubmission(
                suite_id="task-recovery",
                suite_version=1,
                subjects=[
                    EvaluationSubject(
                        model_id=target.target.model_id, variant_id=target.target.variant_id
                    )
                ],
            ),
            node="coire-edge-a",
            resident_targets=[EvaluationResident(instance_id=instance_id, target=target.target)],
            prompt_set_sha256=prompt_digest(),
        ).model_dump(mode="json")
        headers = {"Idempotency-Key": "qualified-intent"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            submitted = await http.post(
                "/api/v1/admin/evaluation-measurements", json=body, headers=headers
            )
            assert submitted.status_code == 202, submitted.text
            app.state.settings.evaluations_enabled = False
            replay = await http.post(
                "/api/v1/admin/evaluation-measurements", json=body, headers=headers
            )
            assert replay.status_code == 202 and replay.json() == submitted.json()
            client.prepare_evaluation_probes.assert_awaited_once()
        async with scope() as session:
            assert (
                await session.scalar(select(func.count()).select_from(EvaluationMeasurementRow))
                == 1
            )
            measurement = await session.get(
                EvaluationMeasurementRow, uuid.UUID(submitted.json()["id"])
            )
            assert measurement is not None
            leases = measurement.execution["leases"]
            assert isinstance(leases, list) and len(leases) == 1
            evaluation = await session.get(EvaluationRunRow, measurement.execution["run_id"])
            assert evaluation is not None and evaluation.measurement_id == measurement.id
            assert evaluation.evidence_reserved_bytes == 8 * 1024**2
            group = await session.get(EvaluationGroupRow, evaluation.group_id)
            assert group is not None and group.origin == "measurement"
    finally:
        await engine.dispose()
