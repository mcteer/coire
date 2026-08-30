"""Node routes for engines."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Response, status

from coire_core.models.engine import (
    EngineStartRequest,
    EngineStatus,
    ReconcileRequest,
    ReconcileResult,
)
from coire_node.deps import EngineDep
from coire_node.engines import BudgetExceeded, CopyMissing, NoFreePort

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/node/engines", tags=["engines"])


@router.get("", response_model=list[EngineStatus])
async def list_engines(engines: EngineDep) -> list[EngineStatus]:
    return engines.statuses()


@router.post("", response_model=EngineStatus)
async def start_engine(
    request: EngineStartRequest, response: Response, engines: EngineDep
) -> EngineStatus:
    """Start an engine, or return the one already serving this model (spec FR-019)."""
    try:
        existing, engine_status = engines.start(
            engine_id=request.engine_id,
            slug=request.slug,
            estimate_bytes=request.estimate_bytes,
            chat_template=request.chat_template,
        )
    except CopyMissing as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except BudgetExceeded as exc:
        # The figures travel with the refusal: "no" without them is not actionable.
        raise HTTPException(status.HTTP_409_CONFLICT, exc.refusal.model_dump(mode="json")) from exc
    except NoFreePort as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    response.status_code = status.HTTP_200_OK if existing else status.HTTP_202_ACCEPTED
    return engine_status


@router.post("/reconcile", response_model=ReconcileResult)
async def reconcile(request: ReconcileRequest, engines: EngineDep) -> ReconcileResult:
    return engines.reconcile(request)


@router.get("/{engine_id}", response_model=EngineStatus)
async def get_engine(engine_id: uuid.UUID, engines: EngineDep) -> EngineStatus:
    found = engines.get(engine_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such engine")
    return found


@router.delete("/{engine_id}", response_model=EngineStatus, status_code=status.HTTP_202_ACCEPTED)
async def stop_engine(engine_id: uuid.UUID, engines: EngineDep) -> EngineStatus:
    stopped = engines.stop(engine_id)
    if stopped is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such engine")
    return stopped
