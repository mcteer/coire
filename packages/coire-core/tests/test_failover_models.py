import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from coire_core.models.failover import (
    ElectionVoteGrant,
    FailoverAccessVerifier,
    FailoverMember,
    FailoverMembershipConfig,
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
        expires_at=now + timedelta(seconds=30),
        signature="signature",
    )
    with pytest.raises(ValidationError):
        PromotionProof(epoch=1, term=1, candidate="coire-edge-a", grants=[grant])


def test_promotion_proof_rejects_replayed_voter_and_expired_grant() -> None:
    now = datetime.now(UTC)
    expired = ElectionVoteGrant(
        epoch=1, term=1, candidate="coire-edge-a", voter="coire-core", expires_at=now, signature="a"
    )
    duplicate = ElectionVoteGrant(
        epoch=1, term=1, candidate="coire-edge-a", voter="coire-core", expires_at=now, signature="b"
    )
    with pytest.raises(ValidationError, match="distinct voters"):
        PromotionProof(epoch=1, term=1, candidate="coire-edge-a", grants=[expired, duplicate])
    valid_peer = ElectionVoteGrant(
        epoch=1,
        term=1,
        candidate="coire-edge-a",
        voter="coire-edge-b",
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


def test_lone_survivor_cannot_form_a_current_quorum() -> None:
    now = datetime.now(UTC)
    only_vote = ElectionVoteGrant(
        epoch=1,
        term=7,
        candidate="coire-edge-b",
        voter="coire-edge-b",
        expires_at=now + timedelta(seconds=5),
        signature="unverified",
    )
    # The contract requires exactly two distinct voters; a single reachable node is an outage.
    with pytest.raises(ValidationError):
        PromotionProof(epoch=1, term=7, candidate="coire-edge-b", grants=[only_vote])
