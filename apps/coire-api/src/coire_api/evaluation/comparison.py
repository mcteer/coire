"""Read-only compatibility projection over immutable execution provenance."""

from sqlalchemy.ext.asyncio import AsyncSession

from coire_core.errors import EvaluationValidationError
from coire_core.models.evaluation import EvaluationComparison, EvaluationResult, SuiteMode


def compare_results(
    left: EvaluationResult,
    right: EvaluationResult,
    *,
    left_subject: int,
    right_subject: int,
) -> EvaluationComparison:
    if not 0 <= left_subject < len(left.subjects) or not 0 <= right_subject < len(right.subjects):
        raise EvaluationValidationError("Comparison subject does not exist in its result")
    reasons: list[str] = []
    if left.outcome != "succeeded" or right.outcome != "succeeded":
        reasons.append("outcome")
    a, b = left.suite, right.suite
    if (a.suite_id, a.version, a.content_sha256) != (b.suite_id, b.version, b.content_sha256):
        reasons.append("suite")
    if (a.template.case_count, a.template.cases_sha256) != (
        b.template.case_count,
        b.template.cases_sha256,
    ):
        reasons.append("cases")
    if (a.template.scorer_version, a.template.mode, a.template.kind) != (
        b.template.scorer_version,
        b.template.mode,
        b.template.kind,
    ):
        reasons.append("scorer")
    if (a.generation, a.judge_generation) != (b.generation, b.judge_generation):
        reasons.append("decoding")
    left_runtime = left.subjects[left_subject].runtime
    right_runtime = right.subjects[right_subject].runtime
    if (left_runtime.engine_version, left_runtime.harness_version, left_runtime.runtime_sha256) != (
        right_runtime.engine_version,
        right_runtime.harness_version,
        right_runtime.runtime_sha256,
    ):
        reasons.append("runtime")
    for reason, field in (
        ("tokenizer", "tokenizer_sha256"),
        ("template", "template_sha256"),
        ("capability", "capability_sha256"),
    ):
        if getattr(left_runtime, field) != getattr(right_runtime, field):
            reasons.append(reason)
    if (a.judge is None) != (b.judge is None) or (
        a.judge is not None
        and b.judge is not None
        and (a.judge.target, a.judge.runtime, a.judge.capability_profile, a.judge.template_override)
        != (b.judge.target, b.judge.runtime, b.judge.capability_profile, b.judge.template_override)
    ):
        reasons.append("judge")
    pairwise = a.template.mode is SuiteMode.PAIRWISE or b.template.mode is SuiteMode.PAIRWISE
    left_score = None if pairwise else left.aggregates[left_subject]
    right_score = None if pairwise else right.aggregates[right_subject]
    comparable = not reasons
    return EvaluationComparison.model_validate(
        {
            "left_result_id": left.id,
            "right_result_id": right.id,
            "left_subject": left_subject,
            "right_subject": right_subject,
            "comparable": comparable,
            "reasons": reasons,
            "left_score": left_score,
            "right_score": right_score,
            "delta": right_score - left_score
            if comparable and left_score is not None and right_score is not None
            else None,
            "pairwise": right.pairwise if pairwise else [],
        }
    )


async def compare_stored_results(
    session: AsyncSession, left_id: str, right_id: str, *, left_subject: int, right_subject: int
) -> EvaluationComparison:
    """Legacy scorecards remain readable, without invented execution provenance."""
    import uuid

    from coire_api.db import EvaluationResultRow, HarnessEvaluationRow
    from coire_core.errors import EvaluationNotFound

    async def lookup(identity: str, subject: int) -> EvaluationResult | None:
        row = await session.get(EvaluationResultRow, identity)
        if row is not None:
            value = EvaluationResult.model_validate(row.result)
            if not 0 <= subject < len(value.subjects):
                raise EvaluationValidationError("Comparison subject does not exist in its result")
            return value
        try:
            legacy_id = uuid.UUID(identity)
        except ValueError:
            raise EvaluationNotFound() from None
        legacy = await session.get(HarnessEvaluationRow, legacy_id)
        if legacy is None:
            raise EvaluationNotFound()
        if subject != 0:
            raise EvaluationValidationError("Legacy scorecard has only one exact subject")
        return None

    left = await lookup(left_id, left_subject)
    right = await lookup(right_id, right_subject)
    if left is not None and right is not None:
        return compare_results(left, right, left_subject=left_subject, right_subject=right_subject)
    return EvaluationComparison.model_validate(
        {
            "left_result_id": left_id,
            "right_result_id": right_id,
            "left_subject": left_subject,
            "right_subject": right_subject,
            "comparable": False,
            "reasons": ["legacy_provenance"],
            "left_score": None,
            "right_score": None,
            "delta": None,
        }
    )
