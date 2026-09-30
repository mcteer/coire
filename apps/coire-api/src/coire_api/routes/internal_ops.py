"""Narrow internal routes available only to the proposing ops service."""

from __future__ import annotations

import json
import uuid
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from opentelemetry import metrics, trace

from coire_api import ops
from coire_api.auth import Principal, require_ops_scope
from coire_api.console.service import project_snapshot
from coire_api.deps import SessionDep, SettingsDep
from coire_api.gateway.provider_budget import (
    ProviderBudgetExceeded,
    reserve_provider_budget,
    settle_provider_budget,
)
from coire_api.gateway.resolution import ModelNotFoundError, resolve_model
from coire_api.ops_tokens import InvalidConfirmation
from coire_core.models.console import ConsoleSnapshot
from coire_core.models.gateway import ChatMessage
from coire_core.models.ops import (
    OpsAnthropicRelayRequest,
    OpsProposalIssued,
    OpsProposalSubmission,
    OpsSession,
    OpsSessionRegistration,
)
from coire_core.models.registry import ModelSource

router = APIRouter(prefix="/api/v1/internal/ops", tags=["internal: ops"])
OpsSessionPrincipal = Annotated[Principal, Depends(require_ops_scope("ops:session"))]
OpsProposalPrincipal = Annotated[Principal, Depends(require_ops_scope("ops:propose"))]
OpsReadPrincipal = Annotated[Principal, Depends(require_ops_scope("ops:read"))]
OpsInferPrincipal = Annotated[Principal, Depends(require_ops_scope("ops:infer"))]
_relay_calls = metrics.get_meter("coire.api.ops").create_counter("coire_ops_provider_relay_total")
_tracer = trace.get_tracer("coire.api.ops")


def _relay_ready(settings: SettingsDep) -> str:
    key = settings.anthropic_api_key.get_secret_value()
    if not settings.ops_enabled or settings.ops_model_source != "anthropic":
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ops provider is disabled")
    if not settings.provider_chat_enabled or not key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ops provider is unavailable")
    return key


@router.get("/anthropic/v1/models/{model_id}")
async def ops_anthropic_model(
    model_id: str, principal: OpsInferPrincipal, settings: SettingsDep
) -> JSONResponse:
    """Health lookup through the API's existing provider egress, with no model payload."""

    if model_id != "claude-sonnet-5-5":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found")
    key = _relay_ready(settings)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"https://api.anthropic.com/v1/models/{model_id}",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ops model unavailable") from exc
    return JSONResponse(content={"id": model_id})


@router.post("/anthropic/v1/messages")
async def ops_anthropic_message(
    body: OpsAnthropicRelayRequest,
    request: Request,
    principal: OpsInferPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """Relay only the pinned, bounded ops tool turn; the provider key stays in the API."""

    key = _relay_ready(settings)
    payload = body.model_dump(mode="json", exclude_none=True)
    if len(json.dumps(payload, separators=(",", ":"))) > 65_536:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "ops turn is too large")
    tool_names = {
        tool.get("name") for tool in body.tools or [] if isinstance(tool.get("name"), str)
    }
    if not tool_names.issubset({"read_snapshot", "propose_reversible_action", "final_result"}):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ops tool is not permitted")
    beta = request.headers.get("anthropic-beta", "")
    if len(beta) > 256:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "provider beta header is too large")
    try:
        model_id = uuid.UUID(settings.ops_model_id)
        if model_id not in principal.permitted_model_ids:
            raise ModelNotFoundError
        resolved = await resolve_model(session, model_id, principal)
        if resolved.source is not ModelSource.ANTHROPIC or resolved.provider_model_id != body.model:
            raise ModelNotFoundError
    except (ValueError, ModelNotFoundError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ops model unavailable") from exc
    request_id = uuid.uuid4()
    try:
        await reserve_provider_budget(
            session,
            resolved,
            [ChatMessage(role="user", content=json.dumps(payload, separators=(",", ":")))],
            body.max_tokens,
            request_id,
        )
    except ProviderBudgetExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    with _tracer.start_as_current_span("coire.api.ops.provider_relay"):
        try:
            async with httpx.AsyncClient(timeout=settings.ops_request_timeout_s) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    json=payload,
                    headers={
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                        **({"anthropic-beta": beta} if beta else {}),
                    },
                )
                response.raise_for_status()
                answer = response.json()
        except httpx.HTTPStatusError as exc:
            _relay_calls.add(1, {"outcome": "refused"})
            try:
                provider_error = exc.response.json()
            except ValueError:
                provider_error = {
                    "error": {"type": "api_error", "message": "provider refused request"}
                }
            return JSONResponse(status_code=exc.response.status_code, content=provider_error)
        except (httpx.HTTPError, ValueError) as exc:
            _relay_calls.add(1, {"outcome": "failed"})
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "ops provider request failed") from exc
    _relay_calls.add(1, {"outcome": "succeeded"})
    if isinstance(answer, dict):
        usage = answer.get("usage")
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                await settle_provider_budget(
                    session, request_id, actual_tokens=input_tokens + output_tokens
                )
                await session.commit()
    return JSONResponse(content=answer)


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
