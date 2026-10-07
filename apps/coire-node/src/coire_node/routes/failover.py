"""Narrow resident-engine discovery, election, and inference relay for the failover frontend."""

from __future__ import annotations

import hmac
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import ValidationError

from coire_core.failover_crypto import verify_ed25519
from coire_core.models.engine import EngineState
from coire_core.models.failover import (
    ElectionVoteGrant,
    ElectionVoteRequest,
    FailoverHeartbeat,
    FailoverOverride,
    FailoverRelayRequest,
    FailoverResidentEngine,
    HandbackNotice,
)
from coire_core.models.gateway import EngineChatRequest
from coire_node.deps import EngineDep, SettingsDep, StoreDep
from coire_node.failover.participant import StudioParticipant
from coire_node.failover.relay import forward_engine_completion

router = APIRouter(prefix="/node/failover", tags=["failover"])
_client: httpx.AsyncClient | None = None
_participant: StudioParticipant | None = None


def set_participant(participant: StudioParticipant | None) -> None:
    """Install the Studio member. Tests and the agent both use this hook."""
    global _participant
    _participant = participant


def get_participant() -> StudioParticipant | None:
    return _participant


def _http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=4)
        )
    return _client


async def _authorize_relay(settings: SettingsDep, token: str | None) -> None:
    expected = settings.failover_relay_token.get_secret_value()
    if not expected or not token or not hmac.compare_digest(expected, token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid failover relay credential")


def _require_participant() -> StudioParticipant:
    if _participant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "failover election is not configured")
    return _participant


@router.get("/resident", response_model=list[FailoverResidentEngine])
async def resident_engines(
    engines: EngineDep,
    settings: SettingsDep,
    x_coire_failover_relay: str | None = Header(default=None),
) -> list[FailoverResidentEngine]:
    await _authorize_relay(settings, x_coire_failover_relay)
    return [
        FailoverResidentEngine(engine_id=item.engine_id, slug=item.slug)
        for item in engines.statuses()
        if item.engine_id is not None
        and item.slug is not None
        and item.state is EngineState.READY
        and (item.target is None or item.target.adapter_id is None)
    ]


@router.post("/heartbeat", response_model=FailoverHeartbeat)
async def heartbeat(request: Request) -> FailoverHeartbeat:
    """Signed liveness exchange. The node bearer is not accepted here."""
    participant = _require_participant()
    try:
        body = FailoverHeartbeat.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "heartbeat signature is invalid") from exc
    if body.member == participant.name or not verify_ed25519(
        body.canonical_bytes(), body.signature, participant.member_public_key(body.member)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "heartbeat signature is invalid")
    return participant.issue_heartbeat(datetime.now(UTC))


@router.post("/election/votes", response_model=ElectionVoteGrant)
async def cast_vote(request: Request) -> ElectionVoteGrant:
    """One signed grant for the priority leader. The node bearer is not accepted here."""
    participant = _require_participant()
    try:
        body = ElectionVoteRequest.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "vote request signature is invalid"
        ) from exc
    if not verify_ed25519(
        body.canonical_bytes(), body.signature, participant.member_public_key(body.candidate)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "vote request signature is invalid")
    grant = participant.vote(body.epoch, body.term, body.candidate, datetime.now(UTC))
    if grant is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "vote refused")
    return grant


@router.post("/election/handback", status_code=status.HTTP_204_NO_CONTENT)
async def handback(request: Request) -> None:
    participant = _require_participant()
    try:
        body = HandbackNotice.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "hand-back signature is invalid") from exc
    if not participant.accept_handback(body, datetime.now(UTC)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "hand-back refused")


@router.post("/election/override", status_code=status.HTTP_204_NO_CONTENT)
async def deliver_override(body: FailoverOverride) -> None:
    """Accept an expiring core-signed override on the control listener."""
    participant = _require_participant()
    if not participant.deliver_override(body):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "override refused")


@router.post("/engines/{engine_id}/chat/completions")
async def relay_completion(
    engine_id: uuid.UUID,
    body: FailoverRelayRequest,
    engines: EngineDep,
    store: StoreDep,
    settings: SettingsDep,
    x_coire_failover_relay: str | None = Header(default=None),
) -> Response:
    await _authorize_relay(settings, x_coire_failover_relay)
    if body.engine_id != engine_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "engine id mismatch")
    engine = engines.get(engine_id)
    if (
        engine is None
        or engine.state is not EngineState.READY
        or engine.slug != body.model_slug
        or (engine.target is not None and engine.target.adapter_id is not None)
        or "@" in str(body.request.model)
    ):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "resident engine is unavailable")
    model_path = str(store.path_for(engine.slug))
    payload = body.request.model_dump(mode="json", exclude_none=True)
    payload["model"] = model_path
    try:
        engine_request = EngineChatRequest.model_validate(payload)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid completion request"
        ) from exc
    try:
        return await forward_engine_completion(
            port=engine.port, request=engine_request, client=_http_client()
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "engine request failed") from exc
