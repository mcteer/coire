"""Partial judging stays inspectable through HTTP without a numeric aggregate."""

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from evaluation_fixtures import FIXTURE, seed_evaluation
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from training_postgres import disposable_postgres

from coire_agent.evaluation import execute_phase
from coire_api.auth import Principal, require_principal
from coire_api.db import (
    AgentRunRow,
    Base,
    EvaluationAttemptRow,
    EvaluationRunRow,
    EvaluationSuiteRow,
    ModelRow,
    ModelVariantRow,
)
from coire_api.evaluation.catalog import build_suite
from coire_api.evaluation.evidence import persist_collected, reserve_quota
from coire_api.evaluation.finalizer import finalize
from coire_api.routes import admin_evaluation_runs
from coire_core.evaluation_suites import cases
from coire_core.models.evaluation import (
    EvaluationGeneration,
    EvaluationOutput,
    EvaluationSubject,
    EvaluationSuiteRegistration,
    EvaluationWorkload,
    canonical_digest,
)
from coire_core.settings import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
def training_postgres_url() -> Iterator[str]:
    with disposable_postgres() as url:
        yield url


async def test_partial_judge_failure_has_null_http_aggregate_and_complete_provenance(
    training_postgres_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            original = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
            judge_model = uuid.uuid4()
            judge = original.target.model_copy(
                update={
                    "public_selector": judge_model,
                    "target": original.target.target.model_copy(
                        update={
                            "model_id": judge_model,
                            "variant_id": uuid.uuid4(),
                            "base_manifest_sha256": "f" * 64,
                        }
                    ),
                }
            )
            suite = build_suite(
                EvaluationSuiteRegistration(
                    suite_id="judge-partial",
                    version=1,
                    template_id="judge-rubric",
                    judge=EvaluationSubject(
                        model_id=judge_model, variant_id=judge.target.variant_id
                    ),
                ),
                owner=run.owner_user_id,
                judge=judge,
                now=datetime.now(UTC),
            )
            catalog = EvaluationSuiteRow(
                id=uuid.uuid4(),
                suite_id=suite.suite_id,
                version=1,
                registry_version=1,
                definition=suite.model_dump(mode="json"),
                content_sha256=suite.content_sha256,
                owner_user_id=run.owner_user_id,
                retired=False,
            )
            session.add(catalog)
            await session.flush()
            run.suite_row_id, run.suite_snapshot, run.state = (
                catalog.id,
                suite.model_dump(mode="json"),
                "running",
            )
            principal = Principal.model_validate(run.authorization_snapshot)
            work = original.model_copy(
                update={
                    "phase": "judge",
                    "suite": suite,
                    "target": judge,
                    "previous_outputs": [
                        EvaluationOutput(
                            case_id=case.id,
                            subject_index=0,
                            text="synthetic response",
                            prompt_tokens=10,
                            completion_tokens=2,
                        )
                        for case in cases("judge-rubric")
                    ],
                }
            )
            work = EvaluationWorkload.model_validate(work.model_dump(mode="json"))
            run.started_deadline_at = work.deadline
            run.started_at = datetime.now(UTC)
            session.add(
                EvaluationAttemptRow(
                    id=work.attempt_id,
                    run_id=run_id,
                    phase="judge",
                    ordinal=1,
                    fence=1,
                    workload=work.model_dump(mode="json"),
                    target_sha256=canonical_digest(judge),
                    deadline_at=work.deadline,
                    state="collecting",
                )
            )
            await reserve_quota(session, run, settings)
            session.add(
                ModelRow(
                    id=judge.target.model_id,
                    repo_id="synthetic/judge-http",
                    slug="judge-http",
                    display_name="Synthetic judge",
                    state="ready",
                    visibility="admin_only",
                    placement_policy="single:auto",
                    memory_estimate_bytes=1024,
                    idle_ttl_seconds=900,
                    precision="bf16",
                    weight_bytes=1024,
                    total_bytes=1024,
                    file_count=2,
                )
            )
            await session.flush()
            session.add(
                ModelVariantRow(
                    id=judge.target.variant_id,
                    model_id=judge.target.model_id,
                    name="fixture",
                    slug="judge-http",
                    precision="bf16",
                    source_revision="synthetic",
                    memory_estimate_bytes=1024,
                    state="ready",
                    validated=True,
                )
            )
            await session.flush()
            session.add(
                AgentRunRow(
                    id=work.run_id,
                    requester_user_id=run.owner_user_id,
                    purpose="evaluation",
                    evaluation_attempt_id=work.attempt_id,
                    profile="general",
                    primary_model_id=judge.target.model_id,
                    primary_variant_id=judge.target.variant_id,
                    workspace_ref=f"eval-{work.run_id}",
                    task_class="read",
                    token_scope={},
                    state="succeeded",
                    limits={},
                )
            )
            await session.flush()
            attempt = await session.get(EvaluationAttemptRow, work.attempt_id)
            assert attempt is not None
            attempt.agent_run_id = work.run_id
            calls = 0

            async def generate(
                system: str, prompt: str, decoding: EvaluationGeneration
            ) -> tuple[str, int, int]:
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise ConnectionError("private unavailable judge transport")
                return '{"correctness":4,"instruction_adherence":4,"clarity":4}', 10, 10

            worker = await execute_phase(work, generate, runtime=judge.runtime)
            assert worker.outcome == "failed" and len(worker.scores) == 1
            evidence_id = await persist_collected(session, work.attempt_id, worker, settings)
            result = await finalize(
                session,
                run_id,
                fence=1,
                outcome="failed",
                reason="model_unavailable",
                evidence=[(work, worker)],
            )
            await session.commit()

        @asynccontextmanager
        async def scope() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session
                await session.commit()

        monkeypatch.setattr(admin_evaluation_runs, "session_scope", scope)
        monkeypatch.setattr("coire_api.evaluation.authorization.session_scope", scope)
        app = FastAPI()
        app.state.settings = settings
        app.include_router(admin_evaluation_runs.router)
        app.dependency_overrides[require_principal] = lambda: principal
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = f"/api/v1/admin/evaluations/{run_id}"
            response = await client.get(path)
            assert response.status_code == 200
            projected = response.json()
            assert projected["result"]["aggregates"] == [None]
            assert projected["result"]["result_sha256"] == result.result_sha256
            assert projected["suite"]["judge"]["target"] == judge.target.model_dump(mode="json")
            assert projected["result"]["harness_verdict"] is None
            raw = await client.get(f"{path}/evidence/{evidence_id}")
            assert raw.status_code == 200
            assert raw.json()["outcome"] == "failed" and len(raw.json()["scores"]) == 1
            assert (
                "private unavailable" not in response.text and "private unavailable" not in raw.text
            )
    finally:
        await engine.dispose()
