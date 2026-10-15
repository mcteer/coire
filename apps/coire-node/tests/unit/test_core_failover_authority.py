"""Normal node inference independently checks Core's epoch-bound serving authority."""

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from coire_core.failover_crypto import sign_ed25519
from coire_core.models.failover import (
    ElectionVoteGrant,
    FailoverAccessVerifier,
    FailoverLease,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverSnapshot,
    PromotionProof,
)
from coire_core.settings import Settings
from coire_node.failover import core_authority


def material(tmp_path: Path) -> tuple[Settings, FailoverLease, FailoverSnapshot, str]:
    now = datetime.now(UTC)
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
        public = base64.b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ).decode()
        members.append(FailoverMember(name=name, priority=priority, public_key=public))
    membership = FailoverMembershipConfig(epoch=1, signing_key_id="core-1", members=members)
    grants = []
    for voter in ("coire-core", "coire-edge-a"):
        unsigned = ElectionVoteGrant(
            epoch=1,
            term=7,
            candidate="coire-core",
            voter=voter,
            issued_at=now,
            expires_at=now + timedelta(seconds=15),
            signature="unsigned",
        )
        grants.append(
            unsigned.model_copy(
                update={"signature": sign_ed25519(unsigned.canonical_bytes(), private[voter])}
            )
        )
    lease = FailoverLease(
        holder="coire-core",
        proof=PromotionProof(epoch=1, term=7, candidate="coire-core", grants=grants),
    )
    unsigned_snapshot = FailoverSnapshot(
        snapshot_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(seconds=120),
        membership=membership,
        access_verifier=FailoverAccessVerifier(
            issuer="https://access.fixture", audience="aud", jwks_url="https://access.fixture/keys"
        ),
        signature="unsigned",
    )
    snapshot = unsigned_snapshot.model_copy(
        update={
            "signature": sign_ed25519(unsigned_snapshot.canonical_bytes(), private["coire-core"])
        }
    )
    path = tmp_path / "snapshot.json"
    path.write_text(snapshot.model_dump_json())
    settings = Settings(
        _secrets_dir="/nonexistent",
        failover_member_name="coire-edge-a",  # type: ignore[call-arg]
        failover_core_public_key=members[0].public_key,
        failover_snapshot_path=str(path),
    )
    return settings, lease, snapshot, private["coire-core"]


def test_missing_or_malformed_proof_cannot_use_static_node_trust(tmp_path: Path) -> None:
    settings, _, _, _ = material(tmp_path)
    for assertion in (None, "{}", "not JSON", "x" * 32_769):
        assert not core_authority.core_authority_guard(settings, assertion)()


def test_core_proof_rechecks_signature_epoch_and_live_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, lease, snapshot, private = material(tmp_path)
    monkeypatch.setattr(core_authority, "node_clock_safe", lambda _: True)
    guard = core_authority.core_authority_guard(settings, lease.model_dump_json())
    assert guard()
    rotated = snapshot.model_copy(
        update={"membership": snapshot.membership.model_copy(update={"epoch": 2})}
    )
    rotated = rotated.model_copy(
        update={"signature": sign_ed25519(rotated.canonical_bytes(), private)}
    )
    Path(settings.failover_snapshot_path).write_text(rotated.model_dump_json())
    assert not guard()


def test_uncertain_node_clock_refuses_even_a_valid_core_proof(tmp_path: Path) -> None:
    settings, lease, _, _ = material(tmp_path)
    assert not core_authority.core_authority_guard(settings, lease.model_dump_json())()


def test_corrupted_snapshot_revokes_an_existing_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, lease, _, _ = material(tmp_path)
    monkeypatch.setattr(core_authority, "node_clock_safe", lambda _: True)
    guard = core_authority.core_authority_guard(settings, lease.model_dump_json())
    assert guard()
    Path(settings.failover_snapshot_path).write_text("corrupt")
    assert not guard()
