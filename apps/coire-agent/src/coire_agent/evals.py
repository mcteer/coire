"""Deterministic harness capability scoring; model judging is intentionally absent."""

from __future__ import annotations

from dataclasses import dataclass

from coire_core.models.harness import CategoryScores, EvaluationVerdict


@dataclass(frozen=True)
class EvaluationEvidence:
    tool_cases_passed: int
    tool_cases_total: int
    output_cases_passed: int
    output_cases_total: int
    edit_cases_passed: int
    edit_cases_total: int
    context_cases_passed: int
    context_cases_total: int


def _ratio(passed: int, total: int) -> float:
    if total <= 0 or passed < 0 or passed > total:
        raise ValueError("evaluation case counts must satisfy 0 <= passed <= total")
    return passed / total


def score(evidence: EvaluationEvidence) -> tuple[CategoryScores, EvaluationVerdict]:
    scores = CategoryScores(
        tool_calling=_ratio(evidence.tool_cases_passed, evidence.tool_cases_total),
        structured_output=_ratio(evidence.output_cases_passed, evidence.output_cases_total),
        edit_application=_ratio(evidence.edit_cases_passed, evidence.edit_cases_total),
        long_context=_ratio(evidence.context_cases_passed, evidence.context_cases_total),
    )
    verdict = (
        EvaluationVerdict.PASSED
        if min(scores.model_dump().values()) >= 0.8
        else EvaluationVerdict.FAILED
    )
    return scores, verdict
