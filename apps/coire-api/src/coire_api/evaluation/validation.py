"""Validate execution-bound receipts; HTTP callers never supply authoritative scores."""

from __future__ import annotations

import math

from coire_core.errors import EvaluationValidationError
from coire_core.evaluation_suites import cases, template
from coire_core.models.evaluation import (
    EvaluationWorkerResult,
    EvaluationWorkload,
    SuiteKind,
    SuiteMode,
    canonical_digest,
)
from coire_core.models.harness import CategoryScores, EvaluationVerdict


def parse_worker_result(value: object) -> EvaluationWorkerResult:
    """Keep private payloads out of exception logs and durable command failures."""
    try:
        return EvaluationWorkerResult.model_validate(value)
    except ValueError:
        raise EvaluationValidationError(
            "Private evaluation evidence has unsupported shape"
        ) from None


def validate_worker_result(workload: EvaluationWorkload, result: EvaluationWorkerResult) -> None:
    binding = (
        workload.evaluation_id,
        workload.attempt_id,
        workload.run_id,
        workload.fence,
        workload.phase,
        canonical_digest(workload),
        workload.suite.content_sha256,
        workload.suite.template.cases_sha256,
        workload.target.runtime,
        workload.target.target,
    )
    received = (
        result.evaluation_id,
        result.attempt_id,
        result.run_id,
        result.fence,
        result.phase,
        result.request_sha256,
        result.suite_sha256,
        result.cases_sha256,
        result.runtime,
        result.target,
    )
    if (
        binding != received
        or template(workload.suite.template.template_id) != workload.suite.template
    ):
        raise EvaluationValidationError("Collected evidence does not match its exact attempt")
    if result.outcome == "succeeded" and (
        result.reason is not None or result.finished_at > workload.deadline
    ):
        raise EvaluationValidationError("Successful evidence exceeds its fixed deadline")
    fixtures = cases(workload.suite.template.template_id)
    case_ids = {case.id for case in fixtures}
    indexes = (
        set(range(workload.subject_count))
        if workload.phase == "judge"
        else {workload.subject_index}
    )
    expected = {(case_id, index) for case_id in case_ids for index in indexes}
    outputs = {(item.case_id, item.subject_index) for item in result.outputs}
    scores = {(item.case_id, item.subject_index) for item in result.scores}
    if (
        len(outputs) != len(result.outputs)
        or len(scores) != len(result.scores)
        or not outputs <= expected
        or not scores <= expected
    ):
        raise EvaluationValidationError("Collected cases are duplicated or foreign")
    contamination = result.contamination
    if contamination is not None:
        if workload.training is None:
            if contamination.status != "not_applicable" or contamination.data_sha256:
                raise EvaluationValidationError("Collected input evidence has no training binding")
        elif (
            contamination.data_sha256
            != [source.source_sha256 for source in workload.training.sources]
            or contamination.status == "not_applicable"
            or not set(contamination.case_ids) <= case_ids
            or (
                contamination.status in ("clean", "overlap")
                and (
                    contamination.checked_cases != len(case_ids)
                    or contamination.selected_rows < 1
                    or contamination.reason is not None
                )
            )
        ):
            raise EvaluationValidationError(
                "Collected input evidence differs from frozen training sources"
            )
    if result.outcome == "succeeded" and workload.training is not None and contamination is None:
        raise EvaluationValidationError("Complete execution requires its bound input scan")
    pair_ids = {item.case_id for item in result.pairwise}
    if len(pair_ids) != len(result.pairwise) or not pair_ids <= case_ids:
        raise EvaluationValidationError("Collected pairwise cases are duplicated or foreign")
    complete = result.outcome == "succeeded"
    if workload.suite.template.kind is not SuiteKind.HARNESS or not complete:
        if result.harness_scores is not None or result.harness_verdict is not None:
            raise EvaluationValidationError("Only complete harness evidence has a gate verdict")
    elif result.harness_scores is None or result.harness_verdict is None:
        raise EvaluationValidationError(
            "Complete harness evidence requires its Studio gate verdict"
        )
    if workload.phase != "judge":
        if result.pairwise or (complete and outputs != expected):
            raise EvaluationValidationError("Candidate output evidence is incomplete")
        if workload.suite.template.kind is SuiteKind.JUDGE:
            if result.scores:
                raise EvaluationValidationError("Candidate phase cannot claim judge scores")
        elif complete and scores != expected:
            raise EvaluationValidationError("Scorer evidence is incomplete")
        for item in result.scores:
            if item.passed is None or item.rubric is not None or item.score != float(item.passed):
                raise EvaluationValidationError("Deterministic scorer evidence is inconsistent")
        if workload.suite.template.kind is SuiteKind.HARNESS and complete:
            measured = {item.case_id: item.score for item in result.scores}
            categories = CategoryScores.model_validate(
                {case.category: measured[case.id] for case in fixtures}
            )
            expected_verdict = (
                EvaluationVerdict.PASSED
                if min(categories.model_dump().values()) >= 0.8
                else EvaluationVerdict.FAILED
            )
            if (
                result.harness_scores != categories
                or result.harness_verdict is not expected_verdict
            ):
                raise EvaluationValidationError("Harness gate evidence is inconsistent")
    elif workload.suite.template.mode is SuiteMode.PAIRWISE:
        if result.outputs or result.scores or (complete and pair_ids != case_ids):
            raise EvaluationValidationError("Pairwise judge evidence is incomplete")
        for pair in result.pairwise:
            first = {"A": 0, "B": 1, "tie": None}[pair.first.winner]
            second = {"A": 1, "B": 0, "tie": None}[pair.second.winner]
            if pair.first_order != "AB":
                first, second = second, first
            winner = first if first == second else None
            if pair.preferred_subject != winner:
                raise EvaluationValidationError("Pairwise presentation evidence is inconsistent")
    else:
        if result.outputs or result.pairwise or (complete and scores != expected):
            raise EvaluationValidationError("Rubric judge evidence is incomplete")
        for item in result.scores:
            if (
                item.passed is not None
                or item.rubric is None
                or item.score is None
                or not math.isclose(
                    item.score, sum(item.rubric.model_dump().values()) / 12, abs_tol=1e-12
                )
            ):
                raise EvaluationValidationError("Rubric evidence is inconsistent")
