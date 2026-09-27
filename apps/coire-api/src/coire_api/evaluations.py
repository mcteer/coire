"""Append-only harness scorecards and exact-variant verification state."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from opentelemetry import metrics
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import HarnessEvaluationRow, ModelVariantRow
from coire_core.models.harness import (
    CategoryScores,
    EvaluationVerdict,
    HarnessEvaluation,
    HarnessEvaluationSubmission,
)

evaluation_regressions = metrics.get_meter("coire.api.harness").create_counter(
    "coire_harness_evaluation_regressions", unit="{regression}"
)


def _projection(row: HarnessEvaluationRow) -> HarnessEvaluation:
    return HarnessEvaluation(
        id=row.id,
        variant_id=row.variant_id,
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
    variant = await session.get(ModelVariantRow, submission.variant_id)
    if variant is None:
        raise LookupError("no such model variant")
    values = list(submission.scores.model_dump().values())
    now = datetime.now(UTC)
    row = HarnessEvaluationRow(
        id=uuid.uuid4(),
        variant_id=submission.variant_id,
        scores=submission.scores.model_dump(mode="json"),
        overall_score=sum(values) / len(values),
        verdict=submission.verdict,
        harness_version=submission.harness_version,
        engine_version=submission.engine_version,
        diagnostics=submission.diagnostics,
        run_at=now,
    )
    session.add(row)
    if submission.verdict is EvaluationVerdict.PASSED:
        variant.harness_verified = True
        variant.harness_verified_at = now
    elif submission.verdict is EvaluationVerdict.FAILED:
        if variant.harness_verified:
            evaluation_regressions.add(1)
        variant.harness_verified = False
        variant.harness_verified_at = None
    await session.flush()
    return _projection(row)


async def list_for_variant(session: AsyncSession, variant_id: uuid.UUID) -> list[HarnessEvaluation]:
    rows = (
        (
            await session.execute(
                select(HarnessEvaluationRow)
                .where(HarnessEvaluationRow.variant_id == variant_id)
                .order_by(HarnessEvaluationRow.run_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_projection(row) for row in rows]


async def get(session: AsyncSession, evaluation_id: uuid.UUID) -> HarnessEvaluation | None:
    row = await session.get(HarnessEvaluationRow, evaluation_id)
    return None if row is None else _projection(row)


async def variant_is_write_verified(session: AsyncSession, variant_id: uuid.UUID) -> bool:
    row = await session.get(ModelVariantRow, variant_id)
    return bool(row and row.harness_verified)
