"""Authenticated administration of harness evaluation scorecards."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from coire_api import evaluations
from coire_api.audit import write_principal_audit
from coire_api.auth import CurrentAdmin
from coire_api.db import ModelVariantRow
from coire_api.deps import SessionDep
from coire_core.models.harness import (
    HarnessEvaluation,
    HarnessEvaluationSubmission,
    HarnessEvaluationTarget,
)
from coire_core.models.registry import CapabilityProfile

router = APIRouter(prefix="/api/v1/admin/harness-evaluations", tags=["admin: evaluations"])


@router.get("/target/{variant_id}", response_model=HarnessEvaluationTarget)
async def evaluation_target(
    variant_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep
) -> HarnessEvaluationTarget:
    variant = await session.get(ModelVariantRow, variant_id)
    if variant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such model variant")
    from coire_api.db import ModelRow

    model = await session.get(ModelRow, variant.model_id)
    if model is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "variant model is missing")
    return HarnessEvaluationTarget(
        variant_id=variant.id,
        model_id=model.id,
        capability_profile=CapabilityProfile.model_validate(model.capability_profile),
    )


@router.post("", response_model=HarnessEvaluation, status_code=status.HTTP_201_CREATED)
async def submit_evaluation(
    body: HarnessEvaluationSubmission, principal: CurrentAdmin, session: SessionDep
) -> HarnessEvaluation:
    try:
        result = await evaluations.record(session, body)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    await write_principal_audit(
        session,
        principal=principal,
        action="harness_evaluation.record",
        target_type="model_variant",
        target_id=str(body.variant_id),
        detail={"evaluation_id": str(result.id), "verdict": result.verdict.value},
    )
    await session.commit()
    return result


@router.get("", response_model=list[HarnessEvaluation])
async def list_evaluations(
    variant_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep
) -> list[HarnessEvaluation]:
    if await session.get(ModelVariantRow, variant_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such model variant")
    return await evaluations.list_for_variant(session, variant_id)


@router.get("/{evaluation_id}", response_model=HarnessEvaluation)
async def get_evaluation(
    evaluation_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep
) -> HarnessEvaluation:
    result = await evaluations.get(session, evaluation_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such harness evaluation")
    return result
