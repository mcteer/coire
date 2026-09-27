"""Admin variant comparison and publication endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response

from coire_api.auth import CurrentAdmin
from coire_api.db import ModelRow, ModelVariantRow
from coire_api.deps import SessionDep, SettingsDep
from coire_api.preconditions import require_current
from coire_api.registry import acquisition, variants
from coire_api.routes.admin_acquisitions import ClientDep, submit_acquisition
from coire_core.models.acquisition import (
    AcquisitionRequest,
    AcquisitionWorkflow,
    ModelVariant,
    VariantPublication,
    VariantRecipe,
)

router = APIRouter(prefix="/api/v1/admin/models/{model_id}/variants", tags=["admin: variants"])


async def _model(session: SessionDep, model_id: uuid.UUID) -> ModelRow:
    row = await session.get(ModelRow, model_id)
    if row is None:
        raise HTTPException(404, "no such model")
    return row


@router.get("", response_model=list[ModelVariant])
async def list_model_variants(
    model_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep
) -> list[ModelVariant]:
    await _model(session, model_id)
    return await variants.list_variants(session, model_id)


@router.post("", response_model=AcquisitionWorkflow, status_code=202)
async def create_model_variant(
    model_id: uuid.UUID,
    body: VariantRecipe,
    request: Request,
    response: Response,
    principal: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
    client: ClientDep,
) -> AcquisitionWorkflow:
    model = await _model(session, model_id)
    return await submit_acquisition(
        AcquisitionRequest(repo_id=model.repo_id, variant=body),
        request,
        response,
        principal,
        session,
        settings,
        client,
    )


@router.patch("/{variant_id}", response_model=ModelVariant)
async def update_model_variant(
    model_id: uuid.UUID,
    variant_id: uuid.UUID,
    body: VariantPublication,
    principal: CurrentAdmin,
    session: SessionDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ModelVariant:
    await _model(session, model_id)
    row = await session.get(ModelVariantRow, variant_id)
    if row is None or row.model_id != model_id:
        raise HTTPException(404, "no such model variant")
    require_current(if_match, row.updated_at)
    try:
        result = await variants.update_publication(
            session, row, body, actor=principal.subject or "admin"
        )
    except acquisition.AcquisitionError as exc:
        await session.commit()
        raise HTTPException(exc.status_code, {"code": exc.code, "detail": exc.detail}) from exc
    await session.commit()
    return result
