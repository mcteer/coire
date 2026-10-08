"""Studio-only typed phase execution with complete installed-catalog evidence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import metrics, trace

from coire_agent.evaluation_judge import MalformedJudge, judge_pair, judge_rubric
from coire_agent.evaluation_tasks import score_case
from coire_core.evaluation_suites import cases, template
from coire_core.models.evaluation import (
    EvaluationCaseScore,
    EvaluationContamination,
    EvaluationGeneration,
    EvaluationOutput,
    EvaluationPairwiseCase,
    EvaluationReason,
    EvaluationRuntime,
    EvaluationWorkerResult,
    EvaluationWorkload,
    SuiteKind,
    SuiteMode,
    canonical_digest,
)

Generate = Callable[[str, str, EvaluationGeneration], Awaitable[tuple[str, int, int]]]
logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.agent.evaluation")
phases = metrics.get_meter("coire.agent.evaluation").create_counter(
    "coire_evaluation_worker_phases_total"
)
generations = metrics.get_meter("coire.agent.evaluation").create_counter(
    "coire_evaluation_case_generations_total"
)


async def execute_phase(
    workload: EvaluationWorkload,
    generate: Generate,
    *,
    runtime: EvaluationRuntime,
    prior_outputs: list[EvaluationOutput] | None = None,
    input_root: Path | None = None,
) -> EvaluationWorkerResult:
    original_generate = generate

    async def measured_generate(
        system: str, prompt: str, generation: EvaluationGeneration
    ) -> tuple[str, int, int]:
        with tracer.start_as_current_span(
            "coire.agent.evaluation.case",
            attributes={"run_id": str(workload.run_id), "phase": workload.phase},
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                result = await original_generate(system, prompt, generation)
            except Exception:
                generations.add(1, {"phase": workload.phase, "outcome": "failed"})
                raise
            generations.add(1, {"phase": workload.phase, "outcome": "completed"})
            return result

    generate = measured_generate
    started = datetime.now(UTC)
    outputs: list[EvaluationOutput] = []
    scores: list[EvaluationCaseScore] = []
    pairs: list[EvaluationPairwiseCase] = []
    contamination: EvaluationContamination | None = None
    reason: EvaluationReason | None = None
    error_type: str | None = None
    outcome: str = "succeeded"
    installed = template(workload.suite.template.template_id)
    if installed != workload.suite.template:
        raise ValueError("suite template differs from installed catalog")
    if runtime != workload.target.runtime:
        raise ValueError("worker runtime differs from admitted identity")
    fixtures = cases(installed.template_id)
    with tracer.start_as_current_span(
        "coire.agent.evaluation.execute", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("run_id", str(workload.run_id))
        span.set_attribute("evaluation_id", workload.evaluation_id)
        span.set_attribute("phase", workload.phase)
        try:
            remaining = (workload.deadline - started).total_seconds()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                if workload.phase == "judge":
                    inputs = (
                        prior_outputs if prior_outputs is not None else workload.previous_outputs
                    )
                    previous = {(item.case_id, item.subject_index): item.text for item in inputs}
                    indexes = list(range(workload.subject_count))
                    if len(previous) != len(inputs) or set(previous) != {
                        (case.id, index) for case in fixtures for index in indexes
                    }:
                        raise ValueError("judge input evidence is incomplete")

                    async def judge_generate(
                        system: str, prompt: str, generation: EvaluationGeneration
                    ) -> str:
                        text, _prompt, _completion = await generate(system, prompt, generation)
                        return text

                    for case in fixtures:
                        if installed.mode is SuiteMode.PAIRWISE:
                            if indexes != [0, 1]:
                                raise ValueError("pairwise judge requires two subjects")
                            pairs.append(
                                await judge_pair(
                                    judge_generate,
                                    case_id=case.id,
                                    prompt=case.prompt,
                                    candidates=(previous[case.id, 0], previous[case.id, 1]),
                                    generation=workload.suite.judge_generation,
                                    deadline=workload.deadline,
                                )
                            )
                        else:
                            for index in indexes:
                                rubric = await judge_rubric(
                                    judge_generate,
                                    prompt=case.prompt,
                                    reference=case.reference or "",
                                    candidate=previous[case.id, index],
                                    generation=workload.suite.judge_generation,
                                    deadline=workload.deadline,
                                )
                                scores.append(
                                    EvaluationCaseScore(
                                        case_id=case.id,
                                        subject_index=index,
                                        rubric=rubric,
                                        score=sum(rubric.model_dump().values()) / 12,
                                    )
                                )
                else:
                    for case in fixtures:
                        text, prompt_tokens, completion_tokens = await generate(
                            "Follow the user's task. Return only the requested response.",
                            case.prompt,
                            workload.suite.generation,
                        )
                        outputs.append(
                            EvaluationOutput(
                                case_id=case.id,
                                subject_index=workload.subject_index,
                                text=text,
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                            )
                        )
                        if installed.kind is not SuiteKind.JUDGE:
                            passed = score_case(case, text)
                            scores.append(
                                EvaluationCaseScore(
                                    case_id=case.id,
                                    subject_index=workload.subject_index,
                                    passed=passed,
                                    score=float(passed),
                                )
                            )
                if workload.training is not None:
                    from coire_agent.evaluation_contamination import scan_training_inputs

                    contamination = (
                        await asyncio.to_thread(scan_training_inputs, workload, input_root)
                        if input_root is not None
                        else EvaluationContamination(
                            status="unavailable",
                            reason="input_missing",
                            data_sha256=[
                                source.source_sha256 for source in workload.training.sources
                            ],
                        )
                    )
        except TimeoutError:
            outcome, reason = "timed_out", "execution_timeout"
        except MalformedJudge:
            outcome, reason = "failed", "malformed_judge"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            error_type = type(error).__name__
            outcome, reason = "failed", "invalid_evidence"
    harness_scores = None
    harness_verdict = None
    if outcome == "succeeded" and installed.kind is SuiteKind.HARNESS:
        from coire_agent.evals import EvaluationEvidence, score

        measured = {case.category: scores[index].passed for index, case in enumerate(fixtures)}
        harness_scores, harness_verdict = score(
            EvaluationEvidence(
                tool_cases_passed=int(bool(measured["tool_calling"])),
                tool_cases_total=1,
                output_cases_passed=int(bool(measured["structured_output"])),
                output_cases_total=1,
                edit_cases_passed=int(bool(measured["edit_application"])),
                edit_cases_total=1,
                context_cases_passed=int(bool(measured["long_context"])),
                context_cases_total=1,
            )
        )
    phases.add(1, {"phase": workload.phase, "outcome": outcome})
    logger.info(
        "evaluation phase completed",
        extra={
            "run_id": str(workload.run_id),
            "evaluation_id": workload.evaluation_id,
            "model_id": str(workload.target.target.model_id),
            "phase": workload.phase,
            "outcome": outcome,
            "safe_reason": reason,
            "error_type": error_type,
        },
    )
    return EvaluationWorkerResult.model_validate(
        {
            "evaluation_id": workload.evaluation_id,
            "attempt_id": workload.attempt_id,
            "run_id": workload.run_id,
            "fence": workload.fence,
            "phase": workload.phase,
            "harness_scores": harness_scores,
            "harness_verdict": harness_verdict,
            "request_sha256": canonical_digest(workload),
            "suite_sha256": workload.suite.content_sha256,
            "cases_sha256": installed.cases_sha256,
            "runtime": runtime,
            "target": workload.target.target,
            "outcome": outcome,
            "reason": reason,
            "outputs": outputs,
            "scores": scores,
            "pairwise": pairs,
            "contamination": contamination,
            "started_at": started,
            "finished_at": datetime.now(UTC),
        }
    )
