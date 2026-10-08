"""One fenced immutable terminal result; only executed harness evidence changes gates."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

from opentelemetry import metrics, trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import EvaluationAttemptRow, EvaluationResultRow
from coire_api.evaluation.authorization import authorize_live_evaluation_action
from coire_api.evaluation.events import append, current_run
from coire_api.evaluation.validation import validate_worker_result
from coire_api.evaluations import record
from coire_api.training.service import training_id
from coire_core.errors import EvaluationConflict, EvaluationForbidden
from coire_core.evaluation_suites import cases
from coire_core.models.evaluation import (
    EvaluationContamination,
    EvaluationReason,
    EvaluationResult,
    EvaluationSuite,
    EvaluationTarget,
    EvaluationWorkerResult,
    EvaluationWorkload,
    SuiteKind,
    SuiteMode,
    canonical_digest,
)
from coire_core.models.harness import CategoryScores, EvaluationVerdict, HarnessEvaluationSubmission

tracer = trace.get_tracer("coire.api.evaluation")
results_total = metrics.get_meter("coire.api.evaluation").create_counter(
    "coire_evaluation_results_total"
)


def result_digest(result: EvaluationResult) -> str:
    return hashlib.sha256(
        json.dumps(
            result.model_dump(mode="json", exclude={"result_sha256"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


async def finalize(
    session: AsyncSession,
    run_id: str,
    *,
    fence: int,
    outcome: Literal["succeeded", "failed", "timed_out", "cancelled"],
    reason: EvaluationReason | None = None,
    evidence: list[tuple[EvaluationWorkload, EvaluationWorkerResult]] | None = None,
) -> EvaluationResult:
    with tracer.start_as_current_span(
        "coire.api.evaluation.finalize", record_exception=False, set_status_on_exception=False
    ):
        snapshot = await current_run(session, run_id, lock=False)
        revoked = False
        try:
            # Match control/coordinator ordering: live owner, then durable run.
            await authorize_live_evaluation_action(
                session, Principal.model_validate(snapshot.authorization_snapshot)
            )
        except EvaluationForbidden:
            revoked = True
        run = await current_run(session, run_id)
        existing = await session.scalar(
            select(EvaluationResultRow).where(EvaluationResultRow.run_id == run_id)
        )
        if existing is not None:
            if existing.fence != fence:
                raise EvaluationConflict("Evaluation terminal fence is stale")
            return EvaluationResult.model_validate(existing.result)
        if run.fence != fence:
            raise EvaluationConflict("Evaluation terminal fence is stale")
        if revoked:
            outcome, reason = "failed", "authorization_revoked"
        suite = EvaluationSuite.model_validate(run.suite_snapshot)
        subjects = [EvaluationTarget.model_validate(subject) for subject in run.subjects]
        receipts = evidence or []
        for workload, worker in receipts:
            validate_worker_result(workload, worker)
            attempt = await session.get(
                EvaluationAttemptRow, workload.attempt_id, populate_existing=True
            )
            if (
                workload.evaluation_id != run.id
                or workload.fence != fence
                or attempt is None
                or attempt.run_id != run.id
                or attempt.fence != fence
                or attempt.agent_run_id != workload.run_id
                or attempt.state not in {"collected", "released"}
                or attempt.collected_sha256
                != hashlib.sha256(worker.model_dump_json().encode()).hexdigest()
                or canonical_digest(EvaluationWorkload.model_validate(attempt.workload))
                != canonical_digest(workload)
            ):
                raise EvaluationConflict("Terminal evidence belongs to another run or fence")
            if (
                workload.suite != suite
                or (workload.phase == "judge" and workload.target != suite.judge)
                or (
                    workload.phase != "judge"
                    and workload.target != subjects[workload.subject_index]
                )
            ):
                raise EvaluationConflict("Terminal evidence subject or suite differs")
        measured = [worker for _workload, worker in receipts]
        scores = [score for worker in measured for score in worker.scores]
        pairs = [pair for worker in measured for pair in worker.pairwise]
        aggregates: list[float | None] = [None] * len(subjects)
        verdict: Literal["passed", "failed"] | None = None
        harness_id = None
        if outcome == "succeeded":
            expected_phases = (
                {"harness"}
                if suite.template.kind is SuiteKind.HARNESS
                else {"base"}
                | ({"candidate"} if len(subjects) == 2 else set())
                | ({"judge"} if suite.template.kind is SuiteKind.JUDGE else set())
            )
            if (
                {worker.phase for worker in measured} != expected_phases
                or len(measured) != len(expected_phases)
                or any(worker.outcome != "succeeded" for worker in measured)
            ):
                raise EvaluationConflict("Successful result requires all declared complete phases")
            expected = {
                (case.id, index)
                for case in cases(suite.template.template_id)
                for index in range(len(subjects))
            }
            if suite.template.mode is SuiteMode.PAIRWISE:
                if len(subjects) != 2 or len(pairs) != suite.template.case_count:
                    raise EvaluationConflict("Pairwise result requires both complete subjects")
                for index in range(2):
                    aggregates[index] = sum(
                        0.5
                        if pair.preferred_subject is None
                        else float(pair.preferred_subject == index)
                        for pair in pairs
                    ) / len(pairs)
            else:
                if {(score.case_id, score.subject_index) for score in scores} != expected or len(
                    scores
                ) != len(expected):
                    raise EvaluationConflict("Measured score cases are incomplete")
                for index in range(len(subjects)):
                    values = [score.score for score in scores if score.subject_index == index]
                    if any(value is None for value in values):
                        raise EvaluationConflict("Measured score is missing")
                    aggregates[index] = sum(value for value in values if value is not None) / len(
                        values
                    )
            if suite.template.kind is SuiteKind.HARNESS:
                if len(subjects) != 1:
                    raise EvaluationConflict("Harness gate requires one exact subject")
                by_id = {score.case_id: score for score in scores}
                categories = {
                    case.category: by_id[case.id].score
                    for case in cases(suite.template.template_id)
                }
                category_scores = CategoryScores.model_validate(categories)
                verdict = (
                    "passed" if min(category_scores.model_dump().values()) >= 0.8 else "failed"
                )
                legacy = await record(
                    session,
                    HarnessEvaluationSubmission(
                        variant_id=subjects[0].target.variant_id,
                        target=subjects[0].target,
                        scores=category_scores,
                        verdict=EvaluationVerdict(verdict),
                        harness_version=subjects[0].runtime.harness_version,
                        engine_version=subjects[0].runtime.engine_version,
                        diagnostics=[],
                    ),
                )
                harness_id = legacy.id
                await write_principal_audit(
                    session,
                    principal=Principal.model_validate(run.authorization_snapshot),
                    action="harness_evaluation.record",
                    target_type="adapter" if subjects[0].target.adapter_id else "model_variant",
                    target_id=str(subjects[0].target.adapter_id or subjects[0].target.variant_id),
                    context={
                        "run_id": run.id,
                        "evaluation_id": str(legacy.id),
                        "verdict": verdict,
                        "target_sha256": canonical_digest(subjects[0].target),
                    },
                )
        now = datetime.now(UTC)
        contamination = next(
            (worker.contamination for worker in measured if worker.contamination is not None),
            EvaluationContamination(status="unavailable", reason="input_missing")
            if run.data_snapshot is not None
            else EvaluationContamination(status="not_applicable", reason="no_training_context"),
        )
        result = EvaluationResult(
            id=training_id(),
            run_id=run.id,
            outcome=outcome,
            reason=reason,
            subjects=subjects,
            suite=suite,
            cases=scores,
            aggregates=aggregates,
            pairwise=pairs,
            harness_verdict=verdict,
            harness_evaluation_id=harness_id,
            contamination=contamination,
            started_at=run.started_at or run.created_at,
            finished_at=now,
            result_sha256="0" * 64,
        )
        result = result.model_copy(update={"result_sha256": result_digest(result)})
        run.state, run.safe_failure_code, run.finished_at = outcome, reason, now
        run.version += 1
        await session.flush()
        session.add(
            EvaluationResultRow(
                id=result.id,
                run_id=run.id,
                fence=fence,
                result=result.model_dump(mode="json"),
                result_sha256=result.result_sha256,
                created_at=now,
            )
        )
        await append(session, run, kind="terminal")
        await write_principal_audit(
            session,
            principal=Principal.model_validate(run.authorization_snapshot),
            action="evaluation.finalize",
            target_type="evaluation_run",
            target_id=run.id,
            context={"outcome": outcome, "reason": reason, "result_sha256": result.result_sha256},
        )
        results_total.add(1, {"kind": suite.template.kind.value, "outcome": outcome})
        await session.flush()
        return result
