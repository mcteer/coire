"""Append-only harness scorecards and exact-variant verification state."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from opentelemetry import metrics, trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import HarnessEvaluationRow, ModelVariantRow, TrainingAdapterRow
from coire_core.models.adapters import InferenceTarget
from coire_core.models.harness import (
    CategoryScores,
    EvaluationVerdict,
    HarnessEvaluation,
    HarnessEvaluationSubmission,
)

evaluation_regressions = metrics.get_meter("coire.api.harness").create_counter(
    "coire_harness_evaluation_regressions", unit="{regression}"
)
evaluations_total = metrics.get_meter("coire.api.harness").create_counter(
    "coire_harness_evaluations_total", unit="1"
)
tracer = trace.get_tracer("coire.api.harness")
logger = logging.getLogger(__name__)


def _projection(
    row: HarnessEvaluationRow, target: InferenceTarget | None = None
) -> HarnessEvaluation:
    return HarnessEvaluation(
        id=row.id,
        variant_id=row.variant_id,
        target=target,
        scores=CategoryScores.model_validate(row.scores),
        overall_score=row.overall_score,
        verdict=row.verdict,
        harness_version=row.harness_version,
        engine_version=row.engine_version,
        diagnostics=row.diagnostics,
        run_at=row.run_at,
    )


async def record(
    session: AsyncSession, submission: HarnessEvaluationSubmission
) -> HarnessEvaluation:
    with tracer.start_as_current_span("coire.api.harness.evaluate") as span:
        span.set_attribute("variant_id", str(submission.variant_id))
        if submission.target and submission.target.adapter_id:
            span.set_attribute("adapter_id", str(submission.target.adapter_id))
        result = await _record(session, submission)
        evaluations_total.add(
            1,
            {
                "verdict": result.verdict.value,
                "subject": "adapter" if result.target and result.target.adapter_id else "base",
            },
        )
        logger.info(
            "harness evaluation recorded",
            extra={
                "evaluation_id": str(result.id),
                "variant_id": str(result.variant_id),
                "adapter_id": str(result.target.adapter_id) if result.target else None,
                "verdict": result.verdict.value,
            },
        )
        return result


async def _record(
    session: AsyncSession, submission: HarnessEvaluationSubmission
) -> HarnessEvaluation:
    variant = await session.get(ModelVariantRow, submission.variant_id, with_for_update=True)
    if variant is None:
        raise LookupError("no such model variant")
    target = submission.target
    subject: ModelVariantRow | TrainingAdapterRow = variant
    if target is not None:
        await validate_target(session, target)
        if target.variant_id != submission.variant_id:
            raise ValueError("evaluation target differs from variant")
        if target.adapter_id is not None:
            adapter = await session.get(TrainingAdapterRow, target.adapter_id, with_for_update=True)
            assert adapter is not None
            subject = adapter
    values = list(submission.scores.model_dump().values())
    now = datetime.now(UTC)
    row = HarnessEvaluationRow(
        id=uuid.uuid4(),
        variant_id=submission.variant_id,
        adapter_id=target.adapter_id if target else None,
        subject_manifest_sha256=(target.adapter_manifest_sha256 or target.base_manifest_sha256)
        if target
        else None,
        scores=submission.scores.model_dump(mode="json"),
        overall_score=sum(values) / len(values),
        verdict=submission.verdict,
        harness_version=submission.harness_version,
        engine_version=submission.engine_version,
        diagnostics=submission.diagnostics,
        run_at=now,
    )
    session.add(row)
    if isinstance(subject, TrainingAdapterRow):
        await session.flush()
    if submission.verdict in {EvaluationVerdict.PASSED, EvaluationVerdict.FAILED}:
        passed = submission.verdict is EvaluationVerdict.PASSED
        was_verified = (
            subject.verified
            if isinstance(subject, TrainingAdapterRow)
            else subject.harness_verified
        )
        if was_verified and not passed:
            evaluation_regressions.add(1)
        if isinstance(subject, TrainingAdapterRow):
            subject.verified = passed
            subject.evaluation_id = row.id
        else:
            subject.harness_verified = passed
            subject.harness_verified_at = now if passed else None
    await session.flush()
    return _projection(row, target)


async def list_for_variant(
    session: AsyncSession, variant_id: uuid.UUID, adapter_id: uuid.UUID | None = None
) -> list[HarnessEvaluation]:
    rows = (
        (
            await session.execute(
                select(HarnessEvaluationRow)
                .where(HarnessEvaluationRow.variant_id == variant_id)
                .where(HarnessEvaluationRow.adapter_id == adapter_id)
                .order_by(HarnessEvaluationRow.run_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [await project_evaluation(session, row) for row in rows]


async def get(session: AsyncSession, evaluation_id: uuid.UUID) -> HarnessEvaluation | None:
    row = await session.get(HarnessEvaluationRow, evaluation_id)
    return None if row is None else await project_evaluation(session, row)


async def project_evaluation(session: AsyncSession, row: HarnessEvaluationRow) -> HarnessEvaluation:
    target = None
    if row.subject_manifest_sha256 is not None:
        variant = await session.get(ModelVariantRow, row.variant_id)
        assert variant is not None
        adapter = await session.get(TrainingAdapterRow, row.adapter_id) if row.adapter_id else None
        target = InferenceTarget(
            model_id=variant.model_id,
            variant_id=row.variant_id,
            adapter_id=row.adapter_id,
            base_manifest_sha256=adapter.base_manifest_sha256
            if adapter
            else row.subject_manifest_sha256,
            adapter_manifest_sha256=row.subject_manifest_sha256 if adapter else None,
        )
    return _projection(row, target)


async def validate_target(session: AsyncSession, target: InferenceTarget) -> None:
    """Reject mismatched artifact identity before reading or changing verification."""
    from coire_api.db import VariantCopyRow

    variant = await session.get(ModelVariantRow, target.variant_id)
    if variant is None or variant.model_id != target.model_id:
        raise LookupError("no such exact model target")
    digests = set(
        (
            await session.scalars(
                select(VariantCopyRow.manifest_sha256).where(
                    VariantCopyRow.variant_id == target.variant_id,
                    VariantCopyRow.verified.is_(True),
                )
            )
        ).all()
    )
    if digests != {target.base_manifest_sha256}:
        raise ValueError("base manifest differs from exact target")
    if target.adapter_id is not None:
        adapter = await session.get(TrainingAdapterRow, target.adapter_id)
        if adapter is None:
            raise LookupError("no such adapter")
        if (
            adapter.model_id != target.model_id
            or adapter.base_variant_id != target.variant_id
            or adapter.base_manifest_sha256 != target.base_manifest_sha256
            or adapter.manifest_sha256 != target.adapter_manifest_sha256
            or adapter.state != "ready"
        ):
            raise ValueError("adapter differs from exact target")


async def target_is_write_verified(session: AsyncSession, target: InferenceTarget) -> bool:
    try:
        await validate_target(session, target)
    except (LookupError, ValueError):
        return False
    if target.adapter_id is None:
        return await variant_is_write_verified(session, target.variant_id)
    adapter = await session.get(TrainingAdapterRow, target.adapter_id)
    return bool(adapter and adapter.verified and adapter.evaluation_id)


async def variant_is_write_verified(session: AsyncSession, variant_id: uuid.UUID) -> bool:
    row = await session.get(ModelVariantRow, variant_id)
    return bool(row and row.harness_verified)
