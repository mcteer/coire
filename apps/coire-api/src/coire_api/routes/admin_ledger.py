"""Authenticated operator routes for placement and memory accounting."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from coire_api.audit import write_audit
from coire_api.auth import CurrentAdmin
from coire_api.db import ModelRow, ModelVariantRow, PlacementDecisionRow
from coire_api.deps import SessionDep, SettingsDep
from coire_api.placement import service
from coire_core.models.audit import AuditAction
from coire_core.models.placement import (
    LedgerUpdate,
    MemoryLedger,
    PinUpdate,
    PlacementDecision,
    PlacementOccupant,
    PlacementRequest,
    PlacementState,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin: placement"])


def _decision(row: PlacementDecisionRow) -> PlacementDecision:
    return PlacementDecision(
        id=row.id,
        model_id=row.model_id,
        variant_id=row.variant_id,
        policy=row.policy,
        required_bytes=row.required_bytes,
        state=row.state,
        selected_node_id=row.selected_node_id,
        evicted_reservation_ids=[uuid.UUID(item) for item in row.evicted_reservation_ids],
        refusal_code=row.refusal_code,
        refusal_detail=row.refusal_detail,
        occupants=[PlacementOccupant.model_validate(item) for item in row.occupants],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/ledger", response_model=list[MemoryLedger])
async def list_ledger(
    principal: CurrentAdmin, session: SessionDep, settings: SettingsDep
) -> list[MemoryLedger]:
    await service.ensure_ledgers(
        session,
        budget_bytes=settings.placement_default_budget_bytes,
        sandbox_bytes=settings.placement_sandbox_bytes,
    )
    await session.commit()
    return await service.project_ledgers(session)


@router.patch("/ledger/{node_id}", response_model=MemoryLedger)
async def patch_ledger(
    node_id: uuid.UUID,
    body: LedgerUpdate,
    principal: CurrentAdmin,
    session: SessionDep,
) -> MemoryLedger:
    try:
        result = await service.update_ledger(
            session, node_id, body, actor=principal.subject or "admin"
        )
    except service.LedgerNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such node ledger") from exc
    await session.commit()
    return result


@router.patch("/ledger/reservations/{reservation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def patch_reservation(
    reservation_id: uuid.UUID,
    body: PinUpdate,
    principal: CurrentAdmin,
    session: SessionDep,
) -> None:
    try:
        await service.set_pin(session, reservation_id, body, actor=principal.subject or "admin")
    except service.LedgerNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such model reservation") from exc
    await session.commit()


@router.post(
    "/models/{model_id}/placement",
    response_model=PlacementDecision,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_placement(
    model_id: uuid.UUID,
    body: PlacementRequest,
    principal: CurrentAdmin,
    session: SessionDep,
) -> PlacementDecision:
    model = await session.get(ModelRow, model_id)
    variant = await session.get(ModelVariantRow, body.variant_id)
    if model is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such model")
    if variant is None or variant.model_id != model_id or not variant.validated:
        raise HTTPException(status.HTTP_409_CONFLICT, "variant is not verified for this model")
    row = PlacementDecisionRow(
        model_id=model_id,
        variant_id=variant.id,
        policy=body.policy or model.placement_policy,
        required_bytes=max(1, variant.memory_estimate_bytes),
        state=PlacementState.REQUESTED,
    )
    session.add(row)
    await session.flush()
    await write_audit(
        session,
        actor=principal.subject or "admin",
        action=AuditAction.PLACEMENT_REQUEST,
        target_type="placement_decision",
        target_id=str(row.id),
        detail={"model_id": str(model_id), "variant_id": str(variant.id), "policy": row.policy},
    )
    await session.commit()
    await session.refresh(row)
    return _decision(row)


@router.get("/placements/{decision_id}", response_model=PlacementDecision)
async def get_placement(
    decision_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep
) -> PlacementDecision:
    row = await session.get(PlacementDecisionRow, decision_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such placement decision")
    return _decision(row)
