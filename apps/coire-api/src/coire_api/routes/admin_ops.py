"""Human-admin conversation and exact confirmation routes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status

from coire_api import ops
from coire_api.auth import CurrentAdmin
from coire_api.console.ops import answer_from_snapshot
from coire_api.console.service import project_snapshot
from coire_api.db import OpsProposalRow
from coire_api.deps import SessionDep, SettingsDep
from coire_api.ops_tokens import InvalidConfirmation
from coire_core.models.ops import (
    OpsConfirmRequest,
    OpsConversation,
    OpsConversationCreate,
    OpsConversationDetail,
    OpsDeclineRequest,
    OpsMessageCreate,
    OpsMessageRole,
    OpsProposal,
    OpsTurnResponse,
    OpsTurnStatus,
)

router = APIRouter(prefix="/api/v1/admin/ops", tags=["admin: ops"])


def _human_user_id(principal: CurrentAdmin) -> uuid.UUID:
    if principal.user_id is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "a human admin identity is required for ops conversations and decisions",
        )
    return principal.user_id


@router.post("/conversations", response_model=OpsConversation, status_code=status.HTTP_201_CREATED)
async def start_conversation(
    body: OpsConversationCreate,
    principal: CurrentAdmin,
    session: SessionDep,
) -> OpsConversation:
    row = await ops.create_conversation(session, admin_user_id=_human_user_id(principal))
    await session.commit()
    return ops.project_conversation(row)


@router.get("/conversations/{conversation_id}", response_model=OpsConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    principal: CurrentAdmin,
    session: SessionDep,
) -> OpsConversationDetail:
    _human_user_id(principal)
    try:
        return await ops.conversation_detail(session, conversation_id)
    except ops.OpsNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ops conversation not found") from exc


@router.post("/conversations/{conversation_id}/messages", response_model=OpsTurnResponse)
async def post_message(
    conversation_id: uuid.UUID,
    body: OpsMessageCreate,
    request: Request,
    principal: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
) -> OpsTurnResponse:
    _human_user_id(principal)
    try:
        await ops.append_message(
            session,
            conversation_id=conversation_id,
            role=OpsMessageRole.ADMIN,
            content=body.question,
        )
    except ops.OpsNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ops conversation not found") from exc
    # The deterministic path is the safe foundation. Model-backed forwarding replaces this
    # branch only after the isolated service has registered a healthy session.
    snapshot = await project_snapshot(request, principal, session, settings)
    answer = answer_from_snapshot(snapshot)
    await ops.append_message(
        session,
        conversation_id=conversation_id,
        role=OpsMessageRole.OPS,
        content=answer.answer,
        degraded=True,
    )
    conversation = await ops.get_conversation(session, conversation_id)
    conversation.degraded = True
    conversation.updated_at = datetime.now(UTC)
    await session.commit()
    return OpsTurnResponse(
        status=OpsTurnStatus.DEGRADED,
        answer=answer.answer,
        observed_at=answer.observed_at,
        degraded=True,
        sources=answer.sources,
    )


@router.get("/proposals/{proposal_id}", response_model=OpsProposal)
async def get_proposal(
    proposal_id: uuid.UUID,
    principal: CurrentAdmin,
    session: SessionDep,
) -> OpsProposal:
    _human_user_id(principal)
    row = await session.get(OpsProposalRow, proposal_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ops proposal not found")
    return ops.project_proposal(row)


@router.post(
    "/proposals/{proposal_id}/confirm",
    response_model=OpsProposal,
    status_code=status.HTTP_202_ACCEPTED,
)
async def confirm_proposal(
    proposal_id: uuid.UUID,
    body: OpsConfirmRequest,
    principal: CurrentAdmin,
    session: SessionDep,
) -> OpsProposal:
    _human_user_id(principal)
    try:
        row = await ops.consume_confirmation(
            session,
            proposal_id=proposal_id,
            presented_token=body.confirm_token,
            presented_action=body.action,
            principal=principal,
        )
    except InvalidConfirmation as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": f"confirmation_{exc.reason}", "detail": "confirmation refused"},
        ) from exc
    await session.commit()
    return ops.project_proposal(row)


@router.post("/proposals/{proposal_id}/decline", response_model=OpsProposal)
async def decline_proposal(
    proposal_id: uuid.UUID,
    body: OpsDeclineRequest,
    principal: CurrentAdmin,
    session: SessionDep,
) -> OpsProposal:
    _human_user_id(principal)
    try:
        row = await ops.decline_proposal(
            session,
            proposal_id=proposal_id,
            principal=principal,
            reason=body.reason,
        )
    except ops.OpsNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ops proposal not found") from exc
    except InvalidConfirmation as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "proposal is no longer pending") from exc
    await session.commit()
    return ops.project_proposal(row)
