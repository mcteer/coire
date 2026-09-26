"""Peer election routes on core. User credentials are not accepted here."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import ValidationError

from coire_api.db import session_scope
from coire_api.deps import SettingsDep
from coire_api.failover.participant import CoreParticipant
from coire_api.failover.reconcile import reconcile_events
from coire_core.failover_crypto import verify_ed25519
from coire_core.failover_snapshot import SnapshotError, load_verified_snapshot
from coire_core.models.failover import (
    ElectionVoteGrant,
    ElectionVoteRequest,
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
async def failover_ready(settings: SettingsDep) -> Response:
    """Election gate for the public load balancer. Process liveness stays on `/ready`."""
    if _participant is None and not settings.failover_peer_key.get_secret_value().strip():
        return Response(status_code=status.HTTP_200_OK)
    if _participant is None or not _participant.frontend_ready(datetime.now(UTC)):
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response(status_code=status.HTTP_200_OK)


@router.post("/api/v1/failover/heartbeat", response_model=FailoverHeartbeat)
async def heartbeat(request: Request) -> FailoverHeartbeat:
    """Signed liveness exchange. A user credential is not accepted here."""
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
        or abs(now - body.sent_at) > timedelta(seconds=30)
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
    if not verify_ed25519(
        body.canonical_bytes(), body.signature, participant.member_public_key(body.candidate)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "vote request signature is invalid")
    grant = participant.vote(body.epoch, body.term, body.candidate, datetime.now(UTC))
    if grant is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "vote refused")
    return grant


@router.post("/api/v1/failover/events")
async def reconcile(request: Request) -> dict[str, int]:
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
        added = await reconcile_events(session, list(body.events))
    return {"reconciled": added}
