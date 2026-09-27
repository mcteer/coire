"""Narrow internal routes available only to the proposing ops service."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from coire_api import ops
from coire_api.auth import Principal, require_ops_scope
from coire_api.console.service import project_snapshot
from coire_api.deps import SessionDep, SettingsDep
from coire_api.ops_tokens import InvalidConfirmation
from coire_core.models.console import ConsoleSnapshot
from coire_core.models.ops import (
    OpsProposalIssued,
    OpsProposalSubmission,
    OpsSession,
    OpsSessionRegistration,
)

router = APIRouter(prefix="/api/v1/internal/ops", tags=["internal: ops"])
OpsSessionPrincipal = Annotated[Principal, Depends(require_ops_scope("ops:session"))]
OpsProposalPrincipal = Annotated[Principal, Depends(require_ops_scope("ops:propose"))]
OpsReadPrincipal = Annotated[Principal, Depends(require_ops_scope("ops:read"))]


@router.get("/snapshot", response_model=ConsoleSnapshot)
async def read_ops_snapshot(
    request: Request,
    principal: OpsReadPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> ConsoleSnapshot:
    """Return the bounded control-plane facts available to the ops model."""

    return await project_snapshot(request, principal, session, settings)


@router.post("/sessions", response_model=OpsSession, status_code=status.HTTP_201_CREATED)
async def register_ops_session(
    body: OpsSessionRegistration,
    principal: OpsSessionPrincipal,
    session: SessionDep,
) -> OpsSession:
    row = await ops.register_session(session, body)
    await session.commit()
    return ops.project_session(row)


@router.patch("/sessions/{session_id}", response_model=OpsSession)
async def heartbeat_ops_session(
    session_id: uuid.UUID,
    principal: OpsSessionPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> OpsSession:
    try:
        row = await ops.heartbeat_session(
            session, session_id, stale_seconds=settings.ops_session_stale_s
        )
    except ops.OpsNotFound as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "ops session is no longer active") from exc
    await session.commit()
    return ops.project_session(row)


@router.post("/proposals", response_model=OpsProposalIssued, status_code=status.HTTP_201_CREATED)
async def submit_ops_proposal(
    body: OpsProposalSubmission,
    principal: OpsProposalPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> OpsProposalIssued:
    try:
        issued = await ops.create_proposal(
            session,
            body,
            ttl_seconds=settings.ops_confirmation_ttl_s,
            stale_seconds=settings.ops_session_stale_s,
        )
    except (ops.OpsNotFound, InvalidConfirmation) as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "proposal context is no longer active"
        ) from exc
    await session.commit()
    return issued
