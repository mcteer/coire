"""Peer election routes on core. User credentials are not accepted here."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import ValidationError

from coire_api.db import session_scope
from coire_api.deps import SettingsDep
from coire_api.failover.participant import CoreParticipant
from coire_api.failover.reconcile import reconcile_events
from coire_core.failover_crypto import sign_ed25519, verify_ed25519
from coire_core.failover_snapshot import SnapshotError, load_verified_snapshot
from coire_core.models.failover import (
    ElectionVoteGrant,
    ElectionVoteRequest,
    FailoverEventAcknowledgement,
    FailoverEventBatch,
    FailoverHeartbeat,
    FailoverSnapshot,
)
from coire_core.settings import get_settings

router = APIRouter(tags=["failover"])
_participant: CoreParticipant | None = None


def set_participant(participant: CoreParticipant | None) -> None:
    global _participant
    _participant = participant


def get_participant() -> CoreParticipant | None:
    return _participant


def _require_participant() -> CoreParticipant:
    if _participant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "failover election is not configured")
    return _participant


@router.get("/failover/ready")
async def failover_ready(
    settings: SettingsDep,
    x_coire_failover_monitor: str | None = Header(default=None),
) -> Response:
    """Election gate for the public load balancer. Process liveness stays on `/ready`."""
    expected = settings.failover_monitor_token.get_secret_value()
    headers = {"Cache-Control": "no-store"}
    if (
        not expected
        or not x_coire_failover_monitor
        or not hmac.compare_digest(expected, x_coire_failover_monitor)
    ):
        return Response(status_code=401, headers=headers)
    if _participant is None and not settings.failover_peer_key.get_secret_value().strip():
        return Response(status_code=200, headers=headers)
    if _participant is None or not _participant.frontend_ready(datetime.now(UTC)):
        return Response(status_code=503, headers=headers)
    return Response(status_code=200, headers=headers)


@router.post("/api/v1/failover/heartbeat", response_model=FailoverHeartbeat)
async def heartbeat(request: Request) -> FailoverHeartbeat:
    """Signed liveness exchange. A user credential is not accepted here."""
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


@router.post("/api/v1/failover/snapshot", response_model=FailoverSnapshot)
async def replicated_snapshot(request: Request) -> FailoverSnapshot:
    """Return the latest signed cache to an authenticated declared Studio."""
    participant = _require_participant()
    try:
        body = FailoverHeartbeat.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid peer signature") from exc
    now = datetime.now(UTC)
    if (
        body.member == "coire-core"
        or body.epoch != participant.membership.epoch
        or abs(now - body.sent_at) > timedelta(seconds=2)
        or not verify_ed25519(
            body.canonical_bytes(), body.signature, participant.member_public_key(body.member)
        )
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid peer signature")
    settings = get_settings()
    try:
        return load_verified_snapshot(
            Path(settings.failover_snapshot_path),
            settings.failover_core_public_key,
            max_age_s=settings.failover_snapshot_max_age_s,
        )
    except SnapshotError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "snapshot_unavailable") from exc


@router.post("/api/v1/failover/votes", response_model=ElectionVoteGrant)
async def cast_vote(request: Request) -> ElectionVoteGrant:
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


@router.post("/api/v1/failover/events", response_model=FailoverEventAcknowledgement)
async def reconcile(request: Request) -> FailoverEventAcknowledgement:
    _require_participant()
    try:
        body = FailoverEventBatch.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "event batch signature is invalid"
        ) from exc
    settings = get_settings()
    try:
        snapshot = load_verified_snapshot(
            Path(settings.failover_snapshot_path),
            settings.failover_core_public_key,
            max_age_s=settings.failover_snapshot_max_age_s,
        )
    except SnapshotError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "snapshot_unavailable") from exc
    key = next(
        member.public_key for member in snapshot.membership.members if member.name == body.host
    )
    if not verify_ed25519(body.canonical_bytes(), body.signature, key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "event batch signature is invalid")
    async with session_scope() as session:
        await reconcile_events(session, list(body.events))
    acknowledgement = FailoverEventAcknowledgement(
        host=body.host,
        batch_id=body.batch_id,
        committed_event_ids=[event.event_id for event in body.events],
        signature="unsigned",
    )
    return acknowledgement.model_copy(
        update={
            "signature": sign_ed25519(
                acknowledgement.canonical_bytes(), settings.failover_peer_key.get_secret_value()
            )
        }
    )
