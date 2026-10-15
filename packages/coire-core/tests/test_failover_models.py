import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from coire_core.models.failover import (
    ElectionVoteGrant,
    FailoverAccessSubject,
    FailoverAccessVerifier,
    FailoverEventAcknowledgement,
    FailoverHeartbeat,
    FailoverLease,
    FailoverLimits,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverOverride,
    FailoverOverrideAcknowledgement,
    FailoverOverrideKind,
    FailoverRelayAuthority,
    FailoverSnapshot,
    PromotionProof,
)


def _membership() -> FailoverMembershipConfig:
    return FailoverMembershipConfig(
        epoch=1,
        signing_key_id="core-1",
        members=[
            FailoverMember(name="coire-core", priority=0, public_key="core"),
            FailoverMember(name="coire-edge-a", priority=1, public_key="edge-a"),
            FailoverMember(name="coire-edge-b", priority=2, public_key="edge-b"),
        ],
    )


def test_promotion_proof_requires_two_grants() -> None:
    now = datetime.now(UTC)
    grant = ElectionVoteGrant(
        epoch=1,
        term=1,
        candidate="coire-edge-a",
        voter="coire-core",
        issued_at=now,
        expires_at=now + timedelta(seconds=30),
        signature="signature",
    )
    with pytest.raises(ValidationError):
        PromotionProof(epoch=1, term=1, candidate="coire-edge-a", grants=[grant])


def test_promotion_proof_rejects_replayed_voter_and_expired_grant() -> None:
    now = datetime.now(UTC)
    expired = ElectionVoteGrant(
        epoch=1,
        term=1,
        candidate="coire-edge-a",
        voter="coire-core",
        issued_at=now - timedelta(seconds=15),
        expires_at=now,
        signature="a",
    )
    duplicate = ElectionVoteGrant(
        epoch=1,
        term=1,
        candidate="coire-edge-a",
        voter="coire-core",
        issued_at=now - timedelta(seconds=15),
        expires_at=now,
        signature="b",
    )
    with pytest.raises(ValidationError, match="distinct voters"):
        PromotionProof(epoch=1, term=1, candidate="coire-edge-a", grants=[expired, duplicate])
    valid_peer = ElectionVoteGrant(
        epoch=1,
        term=1,
        candidate="coire-edge-a",
        voter="coire-edge-b",
        issued_at=now - timedelta(seconds=15),
        expires_at=now,
        signature="b",
    )
    proof = PromotionProof(epoch=1, term=1, candidate="coire-edge-a", grants=[expired, valid_peer])
    assert not proof.is_current(now + timedelta(microseconds=1))


def test_snapshot_requires_a_signature() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        FailoverSnapshot(
            snapshot_id=uuid4(),
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
            membership=_membership(),
            access_verifier=FailoverAccessVerifier(
                issuer="https://team.cloudflareaccess.com",
                audience="aud",
                jwks_url="https://team/cdn-cgi/access/certs",
            ),
            signature="",
        )


def test_snapshot_rejects_expiry_before_issue_and_is_stale_after_expiry() -> None:
    now = datetime.now(UTC)
    kwargs = {
        "snapshot_id": uuid4(),
        "issued_at": now,
        "membership": _membership(),
        "access_verifier": FailoverAccessVerifier(
            issuer="https://team.cloudflareaccess.com",
            audience="aud",
            jwks_url="https://team/cdn-cgi/access/certs",
        ),
        "signature": "signature",
    }
    with pytest.raises(ValidationError, match="after its issue"):
        FailoverSnapshot.model_validate({**kwargs, "expires_at": now})
    snapshot = FailoverSnapshot.model_validate({**kwargs, "expires_at": now + timedelta(seconds=1)})
    assert not snapshot.is_current(now + timedelta(seconds=1))


def test_snapshot_signature_is_verified_against_the_core_membership_key() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()
    membership = _membership().model_copy(
        update={
            "members": [
                FailoverMember(name="coire-core", priority=0, public_key=public_key),
                FailoverMember(name="coire-edge-a", priority=1, public_key="edge-a"),
                FailoverMember(name="coire-edge-b", priority=2, public_key="edge-b"),
            ]
        }
    )
    now = datetime.now(UTC)
    unsigned = FailoverSnapshot(
        snapshot_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=1),
        membership=membership,
        access_verifier=FailoverAccessVerifier(
            issuer="https://team.cloudflareaccess.com",
            audience="aud",
            jwks_url="https://team/cdn-cgi/access/certs",
        ),
        signature="placeholder",
    )
    snapshot = unsigned.model_copy(
        update={
            "signature": base64.b64encode(private_key.sign(unsigned.canonical_bytes())).decode()
        }
    )
    assert snapshot.signature_is_valid(public_key)
    assert not snapshot.model_copy(update={"signature": "invalid"}).signature_is_valid(public_key)
    other = Ed25519PrivateKey.generate()
    other_public = base64.b64encode(
        other.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    assert not snapshot.signature_is_valid(other_public)


def _lease_proof() -> PromotionProof:
    return PromotionProof(
        epoch=1,
        term=1,
        candidate="coire-edge-a",
        grants=[
            ElectionVoteGrant(
                epoch=1,
                term=1,
                candidate="coire-edge-a",
                voter=voter,
                issued_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(seconds=15),
                signature="unverified",
            )
            for voter in ("coire-edge-a", "coire-edge-b")
        ],
    )


def _override(kind: FailoverOverrideKind) -> FailoverOverride:
    return FailoverOverride(
        kind=kind,
        target_host="coire-edge-a",
        epoch=1,
        issued_at=datetime.now(UTC),
        fencing_evidence="other origins independently fenced in isolated fixture",
        actor_id=uuid4(),
        reason="isolated contract test",
        expires_at=datetime.now(UTC) + timedelta(seconds=15),
        signature="unverified",
    )


def test_lease_rejects_simultaneous_quorum_and_break_glass_authority() -> None:
    with pytest.raises(ValidationError, match="exactly one authority"):
        FailoverLease(
            holder="coire-edge-a",
            proof=_lease_proof(),
            break_glass=_override(FailoverOverrideKind.BREAK_GLASS_PROMOTE),
        )


def test_lease_rejects_inhibit_as_serving_authority() -> None:
    with pytest.raises(ValidationError, match="break-glass promotion"):
        FailoverLease(holder="coire-edge-a", break_glass=_override(FailoverOverrideKind.INHIBIT))


def test_lease_accepts_each_single_authority_and_rejects_absent_or_wrong_holder() -> None:
    assert FailoverLease(holder="coire-edge-a", proof=_lease_proof()).proof is not None
    assert (
        FailoverLease(
            holder="coire-edge-a",
            break_glass=_override(FailoverOverrideKind.BREAK_GLASS_PROMOTE),
        ).break_glass
        is not None
    )
    with pytest.raises(ValidationError, match="exactly one authority"):
        FailoverLease(holder="coire-edge-a")
    with pytest.raises(ValidationError, match="proof candidate"):
        FailoverLease(holder="coire-edge-b", proof=_lease_proof())


def test_lone_survivor_cannot_form_a_current_quorum() -> None:
    now = datetime.now(UTC)
    only_vote = ElectionVoteGrant(
        epoch=1,
        term=7,
        candidate="coire-edge-b",
        voter="coire-edge-b",
        issued_at=now,
        expires_at=now + timedelta(seconds=5),
        signature="unverified",
    )
    # The contract requires exactly two distinct voters; a single reachable node is an outage.
    with pytest.raises(ValidationError):
        PromotionProof(epoch=1, term=7, candidate="coire-edge-b", grants=[only_vote])


@pytest.mark.parametrize("version", [0, 1, 3, "2"])
def test_membership_rejects_unsupported_protocol(version: object) -> None:
    with pytest.raises(ValidationError):
        FailoverMembershipConfig.model_validate(
            {**_membership().model_dump(), "protocol_version": version}
        )


def test_membership_rejects_priority_reordering() -> None:
    membership = _membership().model_dump()
    membership["members"][0]["priority"] = 2
    membership["members"][2]["priority"] = 0
    with pytest.raises(ValidationError, match="priorities"):
        FailoverMembershipConfig.model_validate(membership)


@pytest.mark.parametrize("seconds", [0, -1, 301])
def test_grant_rejects_invalid_lifetime(seconds: int) -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="lifetime"):
        ElectionVoteGrant(
            epoch=1,
            term=1,
            candidate="coire-edge-a",
            voter="coire-edge-b",
            issued_at=now,
            expires_at=now + timedelta(seconds=seconds),
            signature="test",
        )


def test_future_grant_cannot_authorize_serving() -> None:
    now = datetime.now(UTC)
    grant = ElectionVoteGrant(
        epoch=1,
        term=1,
        candidate="coire-edge-a",
        voter="coire-edge-b",
        issued_at=now + timedelta(seconds=1),
        expires_at=now + timedelta(seconds=15),
        signature="test",
    )
    assert not grant.is_current(now)


def test_lease_rejects_wrong_override_host_and_unbound_legacy_override() -> None:
    override = _override(FailoverOverrideKind.BREAK_GLASS_PROMOTE)
    with pytest.raises(ValidationError, match="override target"):
        FailoverLease(holder="coire-edge-b", break_glass=override)
    legacy = override.model_dump(exclude={"target_host", "epoch", "issued_at", "fencing_evidence"})
    with pytest.raises(ValidationError):
        FailoverOverride.model_validate(legacy)


@pytest.mark.parametrize("seconds", [0, -1, 301])
def test_override_rejects_unbounded_or_empty_lifetime(seconds: int) -> None:
    override = _override(FailoverOverrideKind.BREAK_GLASS_PROMOTE)
    with pytest.raises(ValidationError, match="lifetime"):
        FailoverOverride.model_validate(
            {**override.model_dump(), "expires_at": override.issued_at + timedelta(seconds=seconds)}
        )


def test_limits_and_budget_dependency_fail_closed() -> None:
    for field, value in (
        ("prompt_tokens", 4097),
        ("output_tokens", 1025),
        ("requests_per_minute", 11),
        ("concurrency", 2),
    ):
        with pytest.raises(ValidationError):
            FailoverLimits.model_validate({field: value})
    with pytest.raises(ValidationError, match="mutable budget"):
        FailoverAccessSubject(
            issuer="https://team.cloudflareaccess.com",
            subject="fixture",
            eligible=True,
            mutable_budget_required=True,
            policy_version=1,
        )


def test_acknowledgement_rejects_duplicate_receipts() -> None:
    event = uuid4()
    with pytest.raises(ValidationError, match="unique"):
        FailoverEventAcknowledgement(
            host="coire-edge-a",
            batch_id=uuid4(),
            signature="fixture",
            committed_event_ids=[event, event],
        )


def test_heartbeat_signature_binds_epoch_and_challenge() -> None:
    beat = FailoverHeartbeat(
        member="coire-edge-a", epoch=1, sent_at=datetime.now(UTC), signature="test"
    )
    assert (
        beat.canonical_bytes() != beat.model_copy(update={"challenge": uuid4()}).canonical_bytes()
    )
    assert beat.canonical_bytes() != beat.model_copy(update={"epoch": 2}).canonical_bytes()


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol_version", 1),
        ("epoch", 0),
        ("host", "coire-unknown"),
        ("request_id", "unbound"),
        ("signature", ""),
    ],
)
def test_override_acknowledgement_rejects_wrong_protocol_or_unbound_identity(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "request_id": uuid4(),
        "host": "coire-edge-a",
        "epoch": 1,
        "accepted": True,
        "signature": "fixture",
    }
    values[field] = value
    with pytest.raises(ValidationError):
        FailoverOverrideAcknowledgement.model_validate(values)


def _relay_authority() -> FailoverRelayAuthority:
    lease = FailoverLease(holder="coire-edge-a", proof=_lease_proof())
    return FailoverRelayAuthority(
        request_id=uuid4(),
        holder=lease.holder,
        epoch=1,
        lease=lease,
        snapshot_id=uuid4(),
        snapshot_digest="a" * 64,
        engine_id=uuid4(),
        model_id=uuid4(),
        request_digest="b" * 64,
        identity_subject="eligible-fixture",
        issued_at=datetime.now(UTC),
        expires_at=lease.authority_expires_at,
        signature="fixture",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("holder", "coire-edge-b"),
        ("epoch", 2),
        ("protocol_version", 1),
        ("snapshot_digest", "unbound"),
        ("request_digest", "unbound"),
        ("identity_subject", ""),
        ("request_id", "not-a-uuid"),
    ],
)
def test_relay_rejects_incoherent_or_unbound_context(field: str, value: object) -> None:
    values = _relay_authority().model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        FailoverRelayAuthority.model_validate(values)


def test_relay_and_drain_cannot_extend_underlying_authority() -> None:
    authority = _relay_authority()
    with pytest.raises(ValidationError, match="extend authority"):
        FailoverRelayAuthority.model_validate(
            {
                **authority.model_dump(),
                "expires_at": authority.lease.authority_expires_at + timedelta(seconds=1),
            }
        )
    with pytest.raises(ValidationError, match="extend authority"):
        FailoverLease.model_validate(
            {
                **authority.lease.model_dump(),
                "phase": "draining",
                "drain_deadline": authority.lease.authority_expires_at + timedelta(seconds=1),
            }
        )
    with pytest.raises(ValidationError, match="drain deadline"):
        FailoverLease.model_validate({**authority.lease.model_dump(), "phase": "draining"})
    with pytest.raises(ValidationError, match="drain deadline"):
        FailoverLease.model_validate(
            {**authority.lease.model_dump(), "drain_deadline": authority.lease.authority_expires_at}
        )
    drain = FailoverLease.model_validate(
        {
            **authority.lease.model_dump(),
            "phase": "draining",
            "drain_deadline": authority.lease.authority_expires_at,
        }
    )
    with pytest.raises(ValidationError, match="serving lease"):
        FailoverRelayAuthority.model_validate({**authority.model_dump(), "lease": drain})


def test_grant_rejects_naive_timestamps() -> None:
    with pytest.raises(ValidationError):
        ElectionVoteGrant(
            epoch=1,
            term=1,
            candidate="coire-edge-a",
            voter="coire-edge-b",
            issued_at=datetime(2026, 10, 14),
            expires_at=datetime(2026, 10, 14, 0, 0, 15),
            signature="test",
        )
