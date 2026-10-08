"""Persisted phase receipts survive fresh sessions without repeating generation.

Synthetic worker transport exercises database orchestration; it is not engine acceptance.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from evaluation_fixtures import FIXTURE, seed_evaluation
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_agent.evaluation import execute_phase
from coire_api.db import (
    AgentRunRow,
    Base,
    EvaluationAttemptRow,
    EvaluationResultRow,
    EvaluationRunRow,
    EvaluationSuiteRow,
    HarnessEvaluationRow,
    ModelRow,
    ModelVariantRow,
)
from coire_api.evaluation.catalog import build_suite
from coire_api.evaluation.evidence import persist_collected, reserve_quota
from coire_core.models.acquisition import VariantState
from coire_core.models.evaluation import (
    EvaluationGeneration,
    EvaluationOutput,
    EvaluationSubject,
    EvaluationSuiteRegistration,
    EvaluationWorkload,
)
from coire_core.models.harness import TaskClass
from coire_core.models.registry import ModelState
from coire_core.models.runs import AgentRunState
from coire_core.settings import Settings
from coire_scheduler.evaluations import advance

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("with_judge", [False, True])
async def test_restart_between_base_candidate_judge_reuses_collected_receipts(
    training_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_judge: bool
) -> None:
    settings = Settings(evaluations_enabled=True, training_dataset_dir=str(tmp_path))
    # Admission is deliberately held until a synthetic worker receipt is supplied.
    admission = AsyncMock(return_value=False)
    monkeypatch.setattr("coire_scheduler.evaluations.reserve_phase", admission)
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        original = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
        base = original.target
        candidate = base.model_copy(
            update={"target": base.target.model_copy(update={"variant_id": uuid.uuid4()})}
        )
        judge_model = uuid.uuid4()
        judge = base.model_copy(
            update={
                "public_selector": judge_model,
                "target": base.target.model_copy(
                    update={
                        "model_id": judge_model,
                        "variant_id": uuid.uuid4(),
                        "base_manifest_sha256": "b" * 64,
                    }
                ),
            }
        )
        async with AsyncSession(engine, expire_on_commit=False) as session:
            identity = await seed_evaluation(session)
            run = await session.get(EvaluationRunRow, identity)
            assert run is not None
            suite = build_suite(
                EvaluationSuiteRegistration(
                    suite_id="judge-restart" if with_judge else "task-restart",
                    version=1,
                    template_id="judge-rubric" if with_judge else "task-coding-instructions",
                    judge=EvaluationSubject(
                        model_id=judge_model, variant_id=judge.target.variant_id
                    )
                    if with_judge
                    else None,
                ),
                owner=run.owner_user_id,
                judge=judge if with_judge else None,
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
            for model_id in (base.target.model_id, judge_model):
                session.add(
                    ModelRow(
                        id=model_id,
                        repo_id=f"synthetic/{model_id}",
                        slug=str(model_id),
                        display_name="Synthetic",
                        state=ModelState.READY,
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
            for target in (base, candidate, judge):
                session.add(
                    ModelVariantRow(
                        id=target.target.variant_id,
                        model_id=target.target.model_id,
                        name=str(target.target.variant_id),
                        slug=str(target.target.variant_id),
                        precision="bf16",
                        source_revision="synthetic",
                        memory_estimate_bytes=1024,
                        state=VariantState.READY,
                        validated=True,
                        harness_verified=True,
                    )
                )
            run.suite_row_id = catalog.id
            run.suite_snapshot = suite.model_dump(mode="json")
            run.subjects = [target.model_dump(mode="json") for target in (base, candidate)]
            await reserve_quota(session, run, settings)
            await session.commit()
        outputs: list[EvaluationOutput] = []
        generation_calls = 0

        async def generate(
            system: str, prompt: str, generation: EvaluationGeneration
        ) -> tuple[str, int, int]:
            nonlocal generation_calls
            generation_calls += 1
            return (
                '{"correctness":4,"instruction_adherence":4,"clarity":4}'
                if "judge" in system.lower()
                else "synthetic candidate",
                10,
                10,
            )

        phases = ("base", "candidate", "judge") if with_judge else ("base", "candidate")
        for ordinal, expected_phase in enumerate(phases, start=1):
            # Each tick uses a newly opened session, as after a scheduler replacement.
            for _ in range(2):
                async with AsyncSession(engine, expire_on_commit=False) as recovered:
                    assert not await advance(recovered, identity, settings)
                    await recovered.commit()
                    attempts = list(
                        (
                            await recovered.scalars(
                                select(EvaluationAttemptRow).order_by(EvaluationAttemptRow.ordinal)
                            )
                        ).all()
                    )
                    assert len(attempts) == ordinal
                    assert attempts[-1].phase == expected_phase
            async with AsyncSession(engine, expire_on_commit=False) as session:
                attempt = await session.get(EvaluationAttemptRow, attempts[-1].id)
                run = await session.get(EvaluationRunRow, identity)
                assert attempt is not None and run is not None
                workload = EvaluationWorkload.model_validate(attempt.workload)
                if run.started_at is None:
                    run.started_at = datetime.now(UTC)
                    run.started_deadline_at = workload.deadline
                run.state = "running"
                child = AgentRunRow(
                    id=workload.run_id,
                    requester_user_id=run.owner_user_id,
                    purpose="evaluation",
                    evaluation_attempt_id=attempt.id,
                    profile="general",
                    primary_model_id=workload.target.target.model_id,
                    primary_variant_id=workload.target.target.variant_id,
                    workspace_ref=f"eval-{workload.run_id}",
                    task_class=TaskClass.READ,
                    token_scope={},
                    state=AgentRunState.SUCCEEDED,
                    limits={},
                )
                session.add(child)
                await session.flush()
                attempt.agent_run_id = child.id
                worker = await execute_phase(
                    workload,
                    generate,
                    runtime=workload.target.runtime,
                    prior_outputs=outputs if expected_phase == "judge" else None,
                )
                assert worker.outcome == "succeeded", worker.reason
                receipt = await persist_collected(session, attempt.id, worker, settings)
                await session.commit()
            calls_before_replay = generation_calls
            async with AsyncSession(engine, expire_on_commit=False) as recovered:
                assert (
                    await persist_collected(recovered, workload.attempt_id, worker, settings)
                    == receipt
                )
                await recovered.commit()
            assert generation_calls == calls_before_replay
            outputs.extend(worker.outputs)
        async with AsyncSession(engine, expire_on_commit=False) as recovered:
            assert await advance(recovered, identity, settings)
            await recovered.commit()
            result = await recovered.scalar(select(EvaluationResultRow))
            assert result is not None and result.result["outcome"] == "succeeded"
            assert result.result["aggregates"] == ([1.0, 1.0] if with_judge else [0.0, 0.0])
            assert (
                result.result["harness_verdict"] is None
                and result.result["harness_evaluation_id"] is None
            )
            variants = (await recovered.scalars(select(ModelVariantRow))).all()
            assert variants and all(variant.harness_verified for variant in variants)
            assert (await recovered.scalars(select(HarnessEvaluationRow))).all() == []
            digest = result.result_sha256
            assert await advance(recovered, identity, settings)
            assert result.result_sha256 == digest and generation_calls == (64 if with_judge else 32)
    finally:
        await engine.dispose()
