"""A Studio retries and atomically accepts only a core-signed snapshot."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from coire_core.failover_crypto import sign_ed25519, verify_ed25519
from coire_core.failover_election import ServiceRole
from coire_core.models.failover import (
    FailoverAccessVerifier,
    FailoverHeartbeat,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverSnapshot,
)
from coire_core.settings import Settings
from coire_node.failover.poller import StudioFailoverController


def _key() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private = base64.b64encode(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    ).decode()
    public = base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    return private, public


@pytest.mark.asyncio
async def test_missing_startup_snapshot_is_retried_and_verified(tmp_path: Path) -> None:
    core_private, core_public = _key()
    edge_private, edge_public = _key()
    _, other_public = _key()
    now = datetime.now(UTC)
    unsigned = FailoverSnapshot(
        snapshot_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        membership=FailoverMembershipConfig(
            epoch=1,
            signing_key_id="core-1",
            members=[
                FailoverMember(name="coire-core", priority=0, public_key=core_public),
                FailoverMember(name="coire-edge-a", priority=1, public_key=edge_public),
                FailoverMember(name="coire-edge-b", priority=2, public_key=other_public),
            ],
        ),
        access_verifier=FailoverAccessVerifier(
            issuer="https://access.example.test",
            audience="aud",
            jwks_url="https://access.example.test/cdn-cgi/access/certs",
        ),
        signature="unsigned",
    )
    snapshot = unsigned.model_copy(
        update={"signature": sign_ed25519(unsigned.canonical_bytes(), core_private)}
    )
    path = tmp_path / "snapshot.json"
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        failover_member_name="coire-edge-a",
        failover_peer_key=SecretStr(edge_private),
        failover_core_public_key=core_public,
        failover_snapshot_path=str(path),
        failover_proof_path=str(tmp_path / "proof.json"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/failover/snapshot"
        heartbeat = FailoverHeartbeat.model_validate_json(request.content)
        assert heartbeat.member == "coire-edge-a"
        assert verify_ed25519(heartbeat.canonical_bytes(), heartbeat.signature, edge_public)
        return httpx.Response(200, json=snapshot.model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        controller = StudioFailoverController(settings, client)
        armed: list[FailoverSnapshot] = []

        async def record(value: FailoverSnapshot) -> None:
            armed.append(value)

        controller._arm = record  # type: ignore[assignment]
        assert await controller.refresh_once()
        accepted_snapshot_id = snapshot.snapshot_id
        older = unsigned.model_copy(
            update={"snapshot_id": uuid4(), "issued_at": now - timedelta(seconds=1)}
        )
        snapshot = older.model_copy(
            update={"signature": sign_ed25519(older.canonical_bytes(), core_private)}
        )
        assert not await controller.refresh_once()
    assert len(armed) == 1
    assert (
        FailoverSnapshot.model_validate_json(path.read_text()).snapshot_id == accepted_snapshot_id
    )
    assert path.stat().st_mode & 0o777 == 0o644


@pytest.mark.asyncio
async def test_forged_snapshot_does_not_replace_the_last_good_copy(tmp_path: Path) -> None:
    core_private, core_public = _key()
    edge_private, edge_public = _key()
    _, other_public = _key()
    now = datetime.now(UTC)
    unsigned = FailoverSnapshot(
        snapshot_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        membership=FailoverMembershipConfig(
            epoch=1,
            signing_key_id="core-1",
            members=[
                FailoverMember(name="coire-core", priority=0, public_key=core_public),
                FailoverMember(name="coire-edge-a", priority=1, public_key=edge_public),
                FailoverMember(name="coire-edge-b", priority=2, public_key=other_public),
            ],
        ),
        access_verifier=FailoverAccessVerifier(
            issuer="https://access.example.test",
            audience="aud",
            jwks_url="https://access.example.test/cdn-cgi/access/certs",
        ),
        signature="unsigned",
    )
    signed = unsigned.model_copy(
        update={"signature": sign_ed25519(unsigned.canonical_bytes(), core_private)}
    )
    path = tmp_path / "snapshot.json"
    path.write_text(signed.model_dump_json())
    forged = signed.model_copy(update={"snapshot_id": uuid4()})
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        failover_member_name="coire-edge-a",
        failover_peer_key=SecretStr(edge_private),
        failover_core_public_key=core_public,
        failover_snapshot_path=str(path),
        failover_proof_path=str(tmp_path / "proof.json"),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=forged.model_dump(mode="json"))
        )
    ) as client:
        assert not await StudioFailoverController(settings, client).refresh_once()
    assert FailoverSnapshot.model_validate_json(path.read_text()).snapshot_id == signed.snapshot_id


@pytest.mark.asyncio
async def test_frontend_runs_only_for_elected_or_draining_role(tmp_path: Path) -> None:
    class FakeDocker:
        def __init__(self) -> None:
            self.running = False
            self.actions: list[str] = []

        async def inspect_container(self, name: str) -> dict[str, object]:
            assert name == "coire-failover"
            return {
                "Config": {"Image": "example@sha256:" + "a" * 64, "User": "65532:65532"},
                "HostConfig": {"ReadonlyRootfs": True, "CapDrop": ["ALL"]},
                "State": {"Running": self.running},
            }

        async def start_container(self, name: str) -> None:
            assert name == "coire-failover"
            self.running = True
            self.actions.append("start")

        async def stop_container(self, name: str) -> None:
            assert name == "coire-failover"
            self.running = False
            self.actions.append("stop")

    class FakeParticipant:
        role = ServiceRole.STANDBY

    class FakePoller:
        participant = FakeParticipant()

        async def stop(self) -> None:
            return None

    docker = FakeDocker()
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        failover_frontend_image="example@sha256:" + "a" * 64,
        failover_proof_path=str(tmp_path / "proof.json"),
    )
    controller = StudioFailoverController(settings, docker=docker)  # type: ignore[arg-type]
    fake_poller = FakePoller()
    controller._poller = fake_poller  # type: ignore[assignment]
    await controller._sync_frontend()
    assert docker.actions == []
    fake_poller.participant.role = ServiceRole.ELECTED
    await controller._sync_frontend()
    fake_poller.participant.role = ServiceRole.DRAINING
    await controller._sync_frontend()
    fake_poller.participant.role = ServiceRole.STANDBY
    await controller._sync_frontend()
    assert docker.actions == ["start", "stop"]
    docker.running = True
    (tmp_path / "proof.json").write_text("old proof")
    await controller.stop()
    assert not (tmp_path / "proof.json").exists()
    assert docker.actions == ["start", "stop", "stop"]
