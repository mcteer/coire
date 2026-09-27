"""Human-admin conversation and exact confirmation routes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, HTTPException, Request, status

from coire_api import ops
from coire_api.audit import write_principal_audit
from coire_api.auth import CurrentAdmin
from coire_api.console.ops import answer_from_snapshot, degraded_action_refusal, is_action_request
from coire_api.console.service import project_snapshot
from coire_api.db import OpsProposalRow
from coire_api.deps import SessionDep, SettingsDep
from coire_api.ops_tokens import InvalidConfirmation
from coire_core.models.audit import AuditOutcome
from coire_core.models.gateway import ProblemDetails
from coire_core.models.ops import (
    OpsConfirmRequest,
    OpsConversation,
    OpsConversationCreate,
    OpsConversationDetail,
    OpsDeclineRequest,
    OpsMessageCreate,
    OpsMessageRole,
    OpsProposal,
    OpsServiceTurnRequest,
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
    settings: SettingsDep,
) -> OpsConversation:
    row = await ops.create_conversation(
        session,
        admin_user_id=_human_user_id(principal),
        stale_seconds=settings.ops_session_stale_s,
    )
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
    active = await ops.current_session(session, stale_seconds=settings.ops_session_stale_s)
    if active is not None:
        try:
            async with httpx.AsyncClient(
                base_url=settings.ops_service_url,
                headers={
                    "Authorization": (f"Bearer {settings.ops_service_token.get_secret_value()}")
                },
                timeout=settings.ops_request_timeout_s,
            ) as client:
                response = await client.post(
                    "/turn",
                    json=OpsServiceTurnRequest(
                        conversation_id=conversation_id,
                        question=body.question,
                    ).model_dump(mode="json"),
                )
                response.raise_for_status()
                turn = OpsTurnResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError):
            pass
        else:
            await ops.append_message(
                session,
                conversation_id=conversation_id,
                role=OpsMessageRole.OPS,
                content=turn.answer,
            )
            conversation = await ops.get_conversation(session, conversation_id)
            conversation.degraded = False
            conversation.updated_at = datetime.now(UTC)
            await session.commit()
            return turn

    snapshot = await project_snapshot(request, principal, session, settings)
    answer = (
        degraded_action_refusal(snapshot)
        if is_action_request(body.question)
        else answer_from_snapshot(snapshot)
    )
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
    responses={status.HTTP_409_CONFLICT: {"model": ProblemDetails}},
)
async def confirm_proposal(
    proposal_id: uuid.UUID,
    body: OpsConfirmRequest,
    principal: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
) -> OpsProposal:
    _human_user_id(principal)
    try:
        row = await ops.consume_confirmation(
            session,
            proposal_id=proposal_id,
            presented_token=body.confirm_token,
            presented_action=body.action,
            principal=principal,
            stale_seconds=settings.ops_session_stale_s,
        )
    except InvalidConfirmation as exc:
        ops.record_confirmation_refusal(proposal_id=proposal_id, reason=exc.reason)
        await write_principal_audit(
            session,
            principal=principal,
            action="ops.confirmation.refused",
            target_type="ops_proposal",
            target_id=str(proposal_id),
            outcome=AuditOutcome.REFUSED,
            detail={"reason": exc.reason},
        )
        await session.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": f"confirmation_{exc.reason}", "detail": "confirmation refused"},
        ) from exc
    # Commit the single-use authority before invoking a mutation. A crash after this point can
    # be reconciled, but can never make the token redeemable again.
    await session.commit()
    row = await ops.execute_confirmed_proposal(
        session,
        proposal_id=proposal_id,
        principal=principal,
        settings=settings,
    )
    await session.commit()
    projected = ops.project_proposal(row)
    if projected.state.value in {"stale", "failed"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": projected.failure_code or "action_refused",
                "detail": (projected.result or {}).get("detail", "confirmed action refused"),
            },
        )
    return projected


@router.post(
    "/proposals/{proposal_id}/decline",
    response_model=OpsProposal,
    responses={status.HTTP_409_CONFLICT: {"model": ProblemDetails}},
)
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
