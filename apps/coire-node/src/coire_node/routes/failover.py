"""Narrow resident-engine discovery, election, and inference relay for the failover frontend."""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.routing import APIRoute
from pydantic import ValidationError

from coire_core.failover_authority import create_relay_authority, lease_current, relay_is_current
from coire_core.failover_crypto import verify_ed25519
from coire_core.failover_election import ServiceRole
from coire_core.failover_identity import AccessIdentityError, verify_access_assertion
from coire_core.failover_limits import (
    FailoverLimitError,
    IdentityCounters,
    bounded_text_request,
    effective_limits,
)
from coire_core.failover_residency import eligible_model
from coire_core.failover_snapshot import SnapshotError, load_verified_snapshot
from coire_core.models.failover import (
    ElectionVoteGrant,
    ElectionVoteRequest,
    FailoverHeartbeat,
    FailoverOverride,
    FailoverOverrideAcknowledgement,
    FailoverRankObservation,
    FailoverRankProbe,
    FailoverRelayAuthority,
    FailoverRelayIssuance,
    FailoverRelayRequest,
    FailoverResidentEngine,
    FailoverSinglePlacement,
    FailoverSnapshot,
    HandbackAcknowledgement,
    HandbackNotice,
)
from coire_core.models.gateway import EngineChatRequest
from coire_node.deps import EngineDep, SettingsDep, StoreDep
from coire_node.deps import get_settings as node_settings
from coire_node.failover.core_authority import holder_authority_current, node_clock_safe
from coire_node.failover.lifetime import bounded_response
from coire_node.failover.participant import StudioParticipant
from coire_node.failover.relay import forward_engine_completion
from coire_node.failover.residency import Residency
from coire_node.maintenance import require_admission


class _BoundedFailoverRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def limited(request: Request) -> Response:
            if (
                request.url.path.endswith("/chat/completions")
                or request.url.path == "/node/failover/authority"
            ):
                await _authorize_relay(
                    await node_settings(request), request.headers.get("X-Coire-Failover-Relay")
                )
            if request.method == "POST":
                raw = bytearray()
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 65536:
                        raise HTTPException(413, "request_limit_exceeded")
                    raw.extend(chunk)
                request._body = bytes(raw)
            return await handler(request)

        return limited


router = APIRouter(prefix="/node/failover", tags=["failover"], route_class=_BoundedFailoverRoute)
_client: httpx.AsyncClient | None = None
_participant: StudioParticipant | None = None
_counters = IdentityCounters()


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


def _snapshot(settings: SettingsDep) -> FailoverSnapshot:
    try:
        snapshot = load_verified_snapshot(
            Path(settings.failover_snapshot_path),
            settings.failover_core_public_key,
            max_age_s=settings.failover_snapshot_max_age_s,
        )
    except SnapshotError as exc:
        raise HTTPException(503, "snapshot_unavailable") from exc
    if snapshot.membership.epoch != settings.failover_membership_epoch or not node_clock_safe(
        settings
    ):
        raise HTTPException(503, "not_elected")
    return snapshot


def _residency(
    request: Request, engines: EngineDep, store: StoreDep, settings: SettingsDep
) -> Residency:
    value = getattr(request.app.state, "failover_residency", None)
    if not isinstance(value, Residency):
        value = Residency(
            settings,
            engines,
            store,
            getattr(request.app.state, "shard_groups", None),
            getattr(request.app.state, "failover_measure_link", None),
        )
        request.app.state.failover_residency = value
    return value


@router.get("/resident", response_model=list[FailoverResidentEngine])
async def resident_engines(
    request: Request,
    engines: EngineDep,
    store: StoreDep,
    settings: SettingsDep,
    x_coire_failover_relay: str | None = Header(default=None),
) -> list[FailoverResidentEngine]:
    await _authorize_relay(settings, x_coire_failover_relay)
    try:
        snapshot = _snapshot(settings)
    except HTTPException:
        return []
    residency = _residency(request, engines, store, settings)
    result = []
    for model in snapshot.models:
        if model.placement is not None and await residency.current(model, snapshot, _http_client()):
            result.append(
                FailoverResidentEngine(
                    engine_id=model.placement.engine_id,
                    slug=model.slug,
                    placement=model.placement,
                )
            )
    return result


@router.post(
    "/rank", response_model=FailoverRankObservation, dependencies=[Depends(require_admission)]
)
async def observe_rank(
    body: FailoverRankProbe,
    request: Request,
    engines: EngineDep,
    store: StoreDep,
    settings: SettingsDep,
) -> FailoverRankObservation:
    participant = _require_participant()
    now = datetime.now(UTC)
    if (
        body.member == participant.name
        or body.epoch != participant.membership.epoch
        or not body.issued_at <= now < body.expires_at
        or not verify_ed25519(
            body.canonical_bytes(), body.signature, participant.member_public_key(body.member)
        )
    ):
        raise HTTPException(401, "invalid rank challenge")
    snapshot = _snapshot(settings)
    if not any(
        eligible_model(model) and model.placement == body.placement for model in snapshot.models
    ):
        raise HTTPException(503, "model_not_resident")
    result = await _residency(request, engines, store, settings).observe(body)
    if result is None:
        raise HTTPException(503, "model_not_resident")
    return result


@router.post("/heartbeat", response_model=FailoverHeartbeat)
async def heartbeat(request: Request) -> FailoverHeartbeat:
    """Signed liveness exchange. The node bearer is not accepted here."""
    participant = _require_participant()
    try:
        body = FailoverHeartbeat.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "heartbeat signature is invalid") from exc
    if (
        body.member == participant.name
        or body.epoch != participant.membership.epoch
        or abs((datetime.now(UTC) - body.sent_at).total_seconds()) > 2.0
    ) or not verify_ed25519(
        body.canonical_bytes(), body.signature, participant.member_public_key(body.member)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "heartbeat signature is invalid")
    return participant.issue_heartbeat(datetime.now(UTC), challenge=body.challenge)


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
    if abs((datetime.now(UTC) - body.issued_at).total_seconds()) > 2.0 or not verify_ed25519(
        body.canonical_bytes(), body.signature, participant.member_public_key(body.candidate)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "vote request signature is invalid")
    grant = participant.vote(
        body.epoch, body.term, body.candidate, datetime.now(UTC), request_id=body.request_id
    )
    if grant is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "vote refused")
    return grant


@router.post("/election/handback", response_model=HandbackAcknowledgement)
async def handback(request: Request) -> HandbackAcknowledgement:
    participant = _require_participant()
    try:
        body = HandbackNotice.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "hand-back signature is invalid") from exc
    if not participant.accept_handback(body, datetime.now(UTC)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "hand-back refused")
    acknowledgement = HandbackAcknowledgement(
        request_id=body.request_id,
        host=participant.name,
        epoch=body.epoch,
        term=body.term,
        successor=body.successor,
        accepted=True,
        fenced=participant.role is ServiceRole.STANDBY,
        expires_at=body.expires_at,
        signature="unsigned",
    )
    return acknowledgement.model_copy(
        update={"signature": participant.sign_payload(acknowledgement.canonical_bytes())}
    )


@router.post("/election/override", response_model=FailoverOverrideAcknowledgement)
async def deliver_override(body: FailoverOverride) -> FailoverOverrideAcknowledgement:
    """Accept an expiring core-signed override on the control listener."""
    participant = _require_participant()
    if not participant.deliver_override(body):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "override refused")
    acknowledgement = FailoverOverrideAcknowledgement(
        host=participant.name,
        epoch=participant.membership.epoch,
        request_id=body.override_id,
        accepted=True,
        signature="unsigned",
    )
    return acknowledgement.model_copy(
        update={"signature": participant.sign_payload(acknowledgement.canonical_bytes())}
    )


@router.post(
    "/authority", response_model=FailoverRelayAuthority, dependencies=[Depends(require_admission)]
)
async def issue_relay_authority(
    body: FailoverRelayIssuance,
    settings: SettingsDep,
    x_coire_failover_relay: str | None = Header(default=None),
) -> FailoverRelayAuthority:
    await _authorize_relay(settings, x_coire_failover_relay)
    participant = _require_participant()
    if not node_clock_safe(settings) or not participant.serving():
        raise HTTPException(503, "not_elected")
    try:
        snapshot = load_verified_snapshot(
            Path(settings.failover_snapshot_path),
            settings.failover_core_public_key,
            max_age_s=settings.failover_snapshot_max_age_s,
        )
    except SnapshotError as exc:
        raise HTTPException(503, "snapshot_unavailable") from exc
    if snapshot.membership.epoch != settings.failover_membership_epoch:
        raise HTTPException(503, "not_elected")
    model = next(
        (
            item
            for item in snapshot.models
            if item.id == body.request.model and not item.entitlement
        ),
        None,
    )
    if (
        model is None
        or not eligible_model(model)
        or model.placement is None
        or model.placement.engine_id != body.engine_id
    ):
        raise HTTPException(503, "model_not_resident")
    try:
        claims = await verify_access_assertion(
            body.access_assertion, snapshot, client=_http_client()
        )
    except AccessIdentityError as exc:
        raise HTTPException(401, "identity_invalid") from exc
    eligible = next(
        item
        for item in snapshot.subjects
        if item.subject == claims["sub"] and item.issuer == claims["iss"]
    )
    try:
        bounded = bounded_text_request(body.request, model, eligible.limits)
    except FailoverLimitError as exc:
        raise HTTPException(
            429 if exc.reason == "request_limit_exceeded" else 503, exc.reason
        ) from exc
    lease = participant.lease()
    # Signing is node-owned and follows identity I/O plus a fresh authority check.
    # Frontend relay credentials are deliberately not signing credentials.
    if (
        bounded != body.request
        or not participant.serving()
        or not lease_current(lease, snapshot, holder=participant.name)
    ):
        raise HTTPException(503, "not_elected")
    if lease is None:
        raise HTTPException(503, "not_elected")
    return create_relay_authority(
        lease=lease,
        snapshot=snapshot,
        engine_id=body.engine_id,
        request=body.request,
        subject=str(claims["sub"]),
        identity_expiry=datetime.fromtimestamp(float(claims["exp"]), UTC),
        private_key=settings.failover_peer_key.get_secret_value(),
    )


@router.post("/engines/{engine_id}/chat/completions", dependencies=[Depends(require_admission)])
async def relay_completion(
    engine_id: uuid.UUID,
    body: FailoverRelayRequest,
    request: Request,
    engines: EngineDep,
    store: StoreDep,
    settings: SettingsDep,
    x_coire_failover_relay: str | None = Header(default=None),
) -> Response:
    await _authorize_relay(settings, x_coire_failover_relay)
    if body.engine_id != engine_id or body.authority.engine_id != engine_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "engine id mismatch")
    try:
        snapshot = load_verified_snapshot(
            Path(settings.failover_snapshot_path),
            settings.failover_core_public_key,
            max_age_s=settings.failover_snapshot_max_age_s,
        )
    except SnapshotError as exc:
        raise HTTPException(503, "snapshot_unavailable") from exc
    if snapshot.membership.epoch != settings.failover_membership_epoch or not node_clock_safe(
        settings
    ):
        raise HTTPException(503, "not_elected")
    if not relay_is_current(body.authority, snapshot, body.request):
        raise HTTPException(503, "not_elected")
    model = next(
        (
            item
            for item in snapshot.models
            if item.id == body.authority.model_id
            and item.slug == body.model_slug
            and not item.entitlement
        ),
        None,
    )
    if model is None or not eligible_model(model):
        raise HTTPException(503, "model_not_resident")
    try:
        claims = await verify_access_assertion(
            body.access_assertion, snapshot, client=_http_client()
        )
    except AccessIdentityError as exc:
        raise HTTPException(401, "identity_invalid") from exc
    if claims["sub"] != body.authority.identity_subject:
        raise HTTPException(401, "identity_invalid")
    eligible = next(
        item
        for item in snapshot.subjects
        if item.subject == claims["sub"] and item.issuer == claims["iss"]
    )
    try:
        bounded = bounded_text_request(body.request, model, eligible.limits)
    except FailoverLimitError as exc:
        raise HTTPException(
            429 if exc.reason == "request_limit_exceeded" else 503, exc.reason
        ) from exc
    if bounded != body.request:
        raise HTTPException(429, "request_limit_exceeded")
    # Recheck after identity/residency I/O, immediately before any bare engine work.
    if not holder_authority_current(
        settings, body.authority.lease, snapshot, existing_request=False
    ):
        raise HTTPException(503, "not_elected")
    if not relay_is_current(body.authority, snapshot, body.request):
        raise HTTPException(503, "not_elected")
    residency = _residency(request, engines, store, settings)
    if not await residency.current(model, snapshot, _http_client()):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "resident engine is unavailable")
    model_path: str | None
    if isinstance(model.placement, FailoverSinglePlacement):
        engine = engines.failover_status(engine_id)
        if engine is None or engine.slug is None:
            raise HTTPException(503, "model_not_resident")
        model_path = str(store.path_for(engine.slug))
        host, port = "127.0.0.1", engine.port
    else:
        groups = residency.groups
        group = groups.get(engine_id) if groups else None
        model_path = groups.model_path(engine_id) if groups else None
        if group is None or model_path is None:
            raise HTTPException(503, "model_not_resident")
        rank_zero = next(rank for rank in group.ranks if rank.rank == 0)
        host, port = rank_zero.host, rank_zero.port
    payload = body.request.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"coire_variant_id", "coire_wait_for_model", "coire_affinity_node"},
    )
    payload["model"] = model_path
    try:
        engine_request = EngineChatRequest.model_validate(payload)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid completion request"
        ) from exc
    try:
        _counters.acquire(str(claims["sub"]), effective_limits(eligible.limits, model))
    except FailoverLimitError as exc:
        raise HTTPException(429, exc.reason) from exc

    async def allowed() -> bool:
        if not node_clock_safe(settings) or not holder_authority_current(
            settings,
            body.authority.lease,
            snapshot,
            existing_request=True,
        ):
            return False
        if not await residency.current(model, snapshot, _http_client()):
            return False
        if datetime.now(UTC).timestamp() >= float(claims["exp"]):
            return False
        try:
            known_snapshot = load_verified_snapshot(
                Path(settings.failover_snapshot_path),
                settings.failover_core_public_key,
                max_age_s=settings.failover_snapshot_max_age_s,
            )
        except SnapshotError:
            return False
        # An already admitted request retains its signed snapshot until its own
        # expiry, but current policy may remove its subject/model immediately.
        return (
            holder_authority_current(
                settings, body.authority.lease, snapshot, existing_request=True
            )
            and relay_is_current(body.authority, snapshot, body.request)
            and known_snapshot.membership.epoch
            == body.authority.epoch
            == settings.failover_membership_epoch
            and any(
                item.id == model.id and eligible_model(item) and item.placement == model.placement
                for item in known_snapshot.models
            )
            and any(
                item.subject == claims["sub"] and item.issuer == claims["iss"] and item.eligible
                for item in known_snapshot.subjects
            )
        )

    try:
        return await bounded_response(
            forward_engine_completion(
                port=port, host=host, request=engine_request, client=_http_client()
            ),
            allowed,
            lambda: _counters.release(str(claims["sub"])),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "engine request failed") from exc
