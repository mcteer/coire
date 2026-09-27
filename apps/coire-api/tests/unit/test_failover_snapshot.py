import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from coire_api.failover.snapshot import SnapshotError, SnapshotPublisher, load_verified_snapshot
from coire_core.models.failover import (
    FailoverAccessVerifier,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverSnapshot,
)


def _private_key_bytes(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    ).decode()


def _snapshot(public_key: str, *, expiry: datetime) -> FailoverSnapshot:
    now = datetime.now(UTC)
    return FailoverSnapshot(
        snapshot_id=uuid4(),
        issued_at=now,
        expires_at=expiry,
        membership=FailoverMembershipConfig(
            epoch=1,
            signing_key_id="core-1",
            members=[
                FailoverMember(name="coire-core", priority=0, public_key=public_key),
                FailoverMember(name="coire-edge-a", priority=1, public_key="edge-a"),
                FailoverMember(name="coire-edge-b", priority=2, public_key="edge-b"),
            ],
        ),
        access_verifier=FailoverAccessVerifier(
            issuer="https://team.cloudflareaccess.com",
            audience="aud",
            jwks_url="https://team/certs",
        ),
        signature="unsigned",
    )


def test_snapshot_publisher_replaces_a_complete_verified_file(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()
    path = tmp_path / "snapshot.json"
    publisher = SnapshotPublisher(path, _private_key_bytes(private))
    published = publisher.publish(
        _snapshot(public, expiry=datetime.now(UTC) + timedelta(minutes=1))
    )
    assert load_verified_snapshot(path, public).snapshot_id == published.snapshot_id
    with pytest.raises(SnapshotError, match="signature"):
        load_verified_snapshot(path, "untrusted-key")
    assert path.stat().st_mode & 0o777 == 0o600


def test_snapshot_loader_fences_tampered_files(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text("{}")
    with pytest.raises(SnapshotError):
        load_verified_snapshot(path, "trusted-key")


def test_snapshot_loader_rejects_a_signed_but_stale_file(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()
    issued = datetime.now(UTC)
    path = tmp_path / "snapshot.json"
    SnapshotPublisher(path, _private_key_bytes(private)).publish(
        _snapshot(public, expiry=issued + timedelta(minutes=5))
    )
    with pytest.raises(SnapshotError, match="stale"):
        load_verified_snapshot(path, public, max_age_s=120, now=issued + timedelta(seconds=121))
