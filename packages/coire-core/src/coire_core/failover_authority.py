"""Origin and node-independent validation of bounded failover relay authority."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from uuid import uuid4

from coire_core.failover_crypto import sign_ed25519, verify_ed25519
from coire_core.failover_residency import eligible_model
from coire_core.models.failover import FailoverLease, FailoverRelayAuthority, FailoverSnapshot
from coire_core.models.gateway import ChatCompletionRequest


def lease_expiry(lease: FailoverLease) -> datetime:
    if lease.proof is not None:
        deadline = min(grant.expires_at for grant in lease.proof.grants)
    elif lease.break_glass is not None:
        deadline = lease.break_glass.expires_at
    else:
        return datetime.min.replace(tzinfo=UTC)
    return min(deadline, lease.drain_deadline) if lease.drain_deadline else deadline


def lease_current(
    lease: FailoverLease | None,
    snapshot: FailoverSnapshot,
    *,
    holder: str | None = None,
    existing_request: bool = False,
    now: datetime | None = None,
) -> bool:
    moment = now or datetime.now(UTC)
    if lease is None or (holder is not None and lease.holder != holder):
        return False
    if lease.phase == "draining" and not existing_request:
        return False
    if lease_expiry(lease) <= moment or not snapshot.is_fresh(120, moment):
        return False
    if lease.proof is not None:
        return lease.proof.is_valid_for(snapshot.membership, moment)
    override = lease.break_glass
    if (
        override is None
        or override.epoch != snapshot.membership.epoch
        or not override.is_current(moment)
    ):
        return False
    core = next(member for member in snapshot.membership.members if member.name == "coire-core")
    return verify_ed25519(override.canonical_bytes(), override.signature, core.public_key)


def request_digest(request: ChatCompletionRequest) -> str:
    return hashlib.sha256(request.model_dump_json().encode()).hexdigest()


def snapshot_digest(snapshot: FailoverSnapshot) -> str:
    return hashlib.sha256(snapshot.canonical_bytes()).hexdigest()


def relay_signature(authority: FailoverRelayAuthority, private_key: str) -> str:
    return sign_ed25519(authority.canonical_bytes(), private_key)


def create_relay_authority(
    *,
    lease: FailoverLease,
    snapshot: FailoverSnapshot,
    engine_id: object,
    request: ChatCompletionRequest,
    subject: str,
    identity_expiry: datetime,
    private_key: str,
) -> FailoverRelayAuthority:
    now = datetime.now(UTC)
    model = next((model for model in snapshot.models if model.id == request.model), None)
    authority = FailoverRelayAuthority.model_validate(
        {
            "request_id": uuid4(),
            "holder": lease.holder,
            "epoch": snapshot.membership.epoch,
            "lease": lease,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_digest": snapshot_digest(snapshot),
            "engine_id": engine_id,
            "model_id": request.model,
            "request_digest": request_digest(request),
            "placement": model.placement if model is not None else None,
            "identity_subject": subject,
            "issued_at": now,
            "expires_at": min(lease_expiry(lease), snapshot.expires_at, identity_expiry),
            "signature": "unsigned",
        }
    )
    return authority.model_copy(update={"signature": relay_signature(authority, private_key)})


def relay_is_current(
    authority: FailoverRelayAuthority,
    snapshot: FailoverSnapshot,
    request: ChatCompletionRequest,
    *,
    now: datetime | None = None,
) -> bool:
    moment = now or datetime.now(UTC)
    model = next((model for model in snapshot.models if model.id == authority.model_id), None)
    expected_placement = model.placement if model is not None else None
    return (
        model is not None
        and eligible_model(model)
        and expected_placement is not None
        and authority.engine_id == expected_placement.engine_id
        and authority.placement == expected_placement
        and authority.issued_at <= moment < authority.expires_at
        and authority.expires_at <= min(snapshot.expires_at, lease_expiry(authority.lease))
        and authority.snapshot_id == snapshot.snapshot_id
        and authority.epoch == snapshot.membership.epoch
        and hmac.compare_digest(authority.snapshot_digest, snapshot_digest(snapshot))
        and hmac.compare_digest(authority.request_digest, request_digest(request))
        and authority.model_id == request.model
        and lease_current(authority.lease, snapshot, holder=authority.holder, now=moment)
        and verify_ed25519(
            authority.canonical_bytes(),
            authority.signature,
            next(
                member.public_key
                for member in snapshot.membership.members
                if member.name == authority.holder
            ),
        )
    )
