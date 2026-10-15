"""A transport credential cannot manufacture another holder's relay authority."""

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from coire_core.failover_authority import create_relay_authority, relay_is_current
from coire_core.failover_crypto import sign_ed25519
from coire_core.models.adapters import InferenceTarget
from coire_core.models.failover import (
    ElectionVoteGrant,
    FailoverAccessVerifier,
    FailoverLease,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverModel,
    FailoverSinglePlacement,
    FailoverSnapshot,
    PromotionProof,
)
from coire_core.models.gateway import ChatCompletionRequest, ChatMessage


def test_only_holder_node_key_can_sign_bound_relay_context() -> None:
    private: dict[str, str] = {}
    members: list[FailoverMember] = []
    for priority, name in enumerate(("coire-core", "coire-edge-a", "coire-edge-b")):
        key = Ed25519PrivateKey.generate()
        private[name] = base64.b64encode(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ).decode()
        members.append(
            FailoverMember(
                name=name,
                priority=priority,
                public_key=base64.b64encode(
                    key.public_key().public_bytes(
                        serialization.Encoding.Raw, serialization.PublicFormat.Raw
                    )
                ).decode(),
            )
        )
    membership = FailoverMembershipConfig(epoch=1, signing_key_id="core-1", members=members)
    now = datetime.now(UTC)
    grants = []
    for voter in ("coire-edge-a", "coire-edge-b"):
        grant = ElectionVoteGrant(
            epoch=1,
            term=1,
            candidate="coire-edge-a",
            voter=voter,
            issued_at=now,
            expires_at=now + timedelta(seconds=15),
            signature="unsigned",
        )
        grants.append(
            grant.model_copy(
                update={"signature": sign_ed25519(grant.canonical_bytes(), private[voter])}
            )
        )
    lease = FailoverLease(
        holder="coire-edge-a",
        proof=PromotionProof(epoch=1, term=1, candidate="coire-edge-a", grants=grants),
    )
    model_id, engine_id = uuid4(), uuid4()
    snapshot = FailoverSnapshot(
        snapshot_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(seconds=120),
        membership=membership,
        models=[
            FailoverModel(
                id=model_id,
                slug="tiny-fixture",
                display_name="Tiny",
                context_window=2048,
                capabilities=frozenset({"text_chat"}),
                placement=FailoverSinglePlacement(
                    instance_id=uuid4(),
                    engine_id=engine_id,
                    node="coire-edge-a",
                    generation="a" * 64,
                    target=InferenceTarget(
                        model_id=model_id, variant_id=uuid4(), base_manifest_sha256="b" * 64
                    ),
                ),
            )
        ],
        access_verifier=FailoverAccessVerifier(
            issuer="https://access.fixture", audience="aud", jwks_url="https://access.fixture/keys"
        ),
        signature="isolated-holder-verification-fixture",
    )
    request = ChatCompletionRequest(
        model=model_id, messages=[ChatMessage(role="user", content="hello")]
    )
    authority = create_relay_authority(
        lease=lease,
        snapshot=snapshot,
        request=request,
        engine_id=engine_id,
        subject="eligible",
        identity_expiry=now + timedelta(seconds=30),
        private_key=private["coire-edge-a"],
    )
    assert relay_is_current(authority, snapshot, request)
    forged = authority.model_copy(
        update={
            "signature": hmac.new(
                b"known-transport-token", authority.canonical_bytes(), hashlib.sha256
            ).hexdigest()
        }
    )
    assert not relay_is_current(forged, snapshot, request)
    wrong_signer = authority.model_copy(
        update={"signature": sign_ed25519(authority.canonical_bytes(), private["coire-edge-b"])}
    )
    assert not relay_is_current(wrong_signer, snapshot, request)
    altered = request.model_copy(
        update={"messages": [ChatMessage(role="user", content="different")]}
    )
    assert not relay_is_current(authority, snapshot, altered)
    assert not relay_is_current(authority, snapshot, request, now=now + timedelta(seconds=16))
