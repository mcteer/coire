"""Composed failover scenarios: core loss, partitions, a lone survivor, and hand-back."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from coire_api.failover.participant import CoreParticipant
from coire_api.failover.snapshot import SnapshotPublisher
from coire_api.routes import failover as core_failover_routes
from coire_core.failover_crypto import sign_ed25519
from coire_core.failover_election import (
    ElectionParticipant,
    ElectionTimings,
    MemberHealth,
    MemberObservation,
    ServiceRole,
)
from coire_core.models.failover import (
    FailoverAccessVerifier,
    FailoverEvent,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverModel,
    FailoverOverride,
    FailoverOverrideKind,
    FailoverRelayRequest,
    FailoverResidentEngine,
    FailoverSnapshot,
    HandbackNotice,
)
from coire_core.settings import Settings
from coire_failover.app import FailoverRuntime, create_app
from coire_node.failover.journal import ElectionJournal
from coire_node.failover.participant import StudioParticipant
from coire_node.failover.poller import StudioFailoverController
from coire_node.routes import failover as node_failover_routes

NAMES = ("coire-core", "coire-edge-a", "coire-edge-b")


def _members() -> tuple[dict[str, str], dict[str, ElectionParticipant], FailoverMembershipConfig]:
    private: dict[str, str] = {}
    public: dict[str, str] = {}
    for name in NAMES:
        key = Ed25519PrivateKey.generate()
        private[name] = base64.b64encode(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ).decode()
        public[name] = base64.b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ).decode()
    membership = FailoverMembershipConfig(
        epoch=1,
        signing_key_id="core-1",
        members=[
            FailoverMember(name=name, priority=index, public_key=public[name])
            for index, name in enumerate(NAMES)
        ],
    )
    timings = ElectionTimings(
        promotion=timedelta(seconds=10),
        demotion=timedelta(seconds=30),
        lease=timedelta(minutes=5),
        drain=timedelta(seconds=30),
    )
    participants = {
        name: ElectionParticipant(
            name=name,
            membership=membership,
            private_key_b64=private[name],
            timings=timings,
            standing_reservation=name != "coire-core",
        )
        for name in NAMES
    }
    return private, participants, membership


def _view(now: datetime, absent: frozenset[str]) -> dict[str, list[MemberObservation]]:
    return {
        name: [
            MemberObservation(
                name=peer,
                health=MemberHealth.HEALTHY
                if peer == name or peer not in absent
                else MemberHealth.UNREACHABLE,
                since=now - timedelta(seconds=20),
            )
            for peer in NAMES
        ]
        for name in NAMES
    }


def _settle(
    participants: dict[str, ElectionParticipant],
    now: datetime,
    *,
    absent: frozenset[str] = frozenset(),
) -> None:
    observations = _view(now, absent)
    for _ in range(3):
        for name, participant in participants.items():
            if name in absent:
                continue
            participant.observe(observations[name], now)
        for candidate in participants.values():
            if candidate.name in absent or candidate.role not in (
                ServiceRole.CANDIDATE,
                ServiceRole.ELECTED,
            ):
                continue
            for voter in participants.values():
                if voter.name in absent or voter.name == candidate.name:
                    continue
                grant = voter.vote(1, candidate.term, candidate.name, now)
                if grant is not None:
                    candidate.receive_grant(grant, now)


def test_core_loss_elects_only_edge_a() -> None:
    _, participants, _ = _members()
    now = datetime.now(UTC)
    _settle(participants, now, absent=frozenset({"coire-core"}))
    assert participants["coire-edge-a"].serving(now)
    assert not participants["coire-edge-b"].serving(now)
    assert not participants["coire-core"].serving(now)


def test_a_partitioned_studio_fences_itself() -> None:
    _, participants, _ = _members()
    now = datetime.now(UTC)
    _settle(participants, now, absent=frozenset({"coire-core"}))
    edge_a = participants["coire-edge-a"]
    assert edge_a.serving(now)
    edge_a.observe(_view(now, frozenset({"coire-core", "coire-edge-b"}))["coire-edge-a"], now)
    assert not edge_a.serving(now)
    assert edge_a.role is ServiceRole.STANDBY


def test_lone_edge_b_does_not_auto_promote() -> None:
    private, participants, _ = _members()
    now = datetime.now(UTC)
    alone = _view(now, frozenset({"coire-core", "coire-edge-a"}))["coire-edge-b"]
    edge_b = participants["coire-edge-b"]
    assert edge_b.observe(alone, now) is ServiceRole.STANDBY
    unsigned = FailoverOverride(
        kind=FailoverOverrideKind.BREAK_GLASS_PROMOTE,
        actor_id=uuid4(),
        reason="operator break glass",
        expires_at=now + timedelta(minutes=5),
        signature="unsigned",
    )
    override = unsigned.model_copy(
        update={"signature": sign_ed25519(unsigned.canonical_bytes(), private["coire-core"])}
    )
    assert edge_b.observe(alone, now, override=override) is ServiceRole.ELECTED
    assert (
        edge_b.observe(alone, now + timedelta(minutes=6), override=override) is ServiceRole.STANDBY
    )


def test_core_reclaim_waits_out_a_flap_then_edge_a_drains() -> None:
    _, participants, _ = _members()
    now = datetime.now(UTC)
    _settle(participants, now, absent=frozenset({"coire-core"}))
    edge_a = participants["coire-edge-a"]
    core = participants["coire-core"]
    flap = _view(now, frozenset())
    for name in NAMES:
        flap[name][0] = MemberObservation(
            name="coire-core", health=MemberHealth.HEALTHY, since=now - timedelta(seconds=5)
        )
    assert edge_a.observe(flap["coire-edge-a"], now) is ServiceRole.ELECTED
    assert not core.serving(now)
    later = now + timedelta(seconds=30)
    for name in NAMES:
        flap[name][0] = MemberObservation(
            name="coire-core", health=MemberHealth.HEALTHY, since=now - timedelta(seconds=30)
        )
    assert edge_a.observe(flap["coire-edge-a"], later, in_flight=1) is ServiceRole.DRAINING
    assert edge_a.observe(flap["coire-edge-a"], later, in_flight=0) is ServiceRole.STANDBY
    core._outage = True
    core._reclaim_since = now
    _settle(participants, later)
    assert core.serving(later)


@pytest.mark.asyncio
async def test_elected_edge_refuses_an_anonymous_completion() -> None:
    private, participants, membership = _members()
    now = datetime.now(UTC)
    _settle(participants, now, absent=frozenset({"coire-core"}))
    lease = participants["coire-edge-a"].lease()
    assert lease is not None
    model = FailoverModel(
        id=uuid4(), slug="tiny-test-model", display_name="Tiny", context_window=32
    )
    unsigned = FailoverSnapshot(
        snapshot_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        membership=membership,
        models=[model],
        access_verifier=FailoverAccessVerifier(
            issuer="https://team.cloudflareaccess.com",
            audience="aud",
            jwks_url="https://team/certs",
        ),
        signature="unsigned",
    )
    snapshot = unsigned.model_copy(
        update={"signature": sign_ed25519(unsigned.canonical_bytes(), private["coire-core"])}
    )
    engine = FailoverResidentEngine(engine_id=uuid4(), slug=model.slug)

    async def relay(body: FailoverRelayRequest, destination: str) -> JSONResponse:
        assert destination == "local"
        assert body.model_slug == model.slug
        return JSONResponse({"id": "completion", "choices": []})

    runtime = FailoverRuntime(
        load_snapshot=lambda: snapshot,
        load_lease=lambda: lease,
        local_resident=lambda: _one(engine),
        relay=relay,
        access_client=None,
    )
    app = create_app(runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(
            "/v1/chat/completions",
            json={"model": str(model.id), "messages": [{"role": "user", "content": "hi"}]},
        )
    assert refused.status_code == 401
    assert refused.headers["x-coire-persistence"] == "unavailable"


async def _one(engine: FailoverResidentEngine) -> list[FailoverResidentEngine]:
    return [engine]


@pytest.mark.asyncio
async def test_studio_subscribes_to_the_core_snapshot_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private, _, membership = _members()
    core_public = next(
        member.public_key for member in membership.members if member.name == "coire-core"
    )
    now = datetime.now(UTC)
    core_path = tmp_path / "core" / "snapshot.json"
    edge_path = tmp_path / "edge" / "snapshot.json"
    core_settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        failover_member_name="coire-core",
        failover_peer_key=SecretStr(private["coire-core"]),
        failover_core_public_key=core_public,
        failover_snapshot_path=str(core_path),
    )
    edge_settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        failover_member_name="coire-edge-a",
        failover_peer_key=SecretStr(private["coire-edge-a"]),
        failover_core_public_key=core_public,
        failover_snapshot_path=str(edge_path),
        failover_proof_path=str(tmp_path / "edge" / "proof.json"),
    )
    snapshot = FailoverSnapshot(
        snapshot_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        membership=membership,
        access_verifier=FailoverAccessVerifier(
            issuer="https://team.cloudflareaccess.com",
            audience="aud",
            jwks_url="https://team.cloudflareaccess.com/cdn-cgi/access/certs",
        ),
        signature="unsigned",
    )
    SnapshotPublisher(core_path, private["coire-core"]).publish(snapshot)
    core_member = CoreParticipant(
        name="coire-core",
        membership=membership,
        private_key_b64=private["coire-core"],
        timings=ElectionTimings(
            promotion=timedelta(seconds=10),
            demotion=timedelta(seconds=30),
            lease=timedelta(seconds=15),
            drain=timedelta(seconds=30),
        ),
    )
    core_failover_routes.set_participant(core_member)
    monkeypatch.setattr(core_failover_routes, "get_settings", lambda: core_settings)
    app = FastAPI()
    app.include_router(core_failover_routes.router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://coire-core"
        ) as client:
            controller = StudioFailoverController(edge_settings, client)
            assert await controller.refresh_once()
            assert controller._poller is not None
            assert (
                FailoverSnapshot.model_validate_json(edge_path.read_text()).snapshot_id
                == snapshot.snapshot_id
            )
            unsigned_override = FailoverOverride(
                kind=FailoverOverrideKind.INHIBIT,
                actor_id=uuid4(),
                reason="pause election during maintenance",
                expires_at=now + timedelta(minutes=1),
                signature="unsigned",
            )
            override = unsigned_override.model_copy(
                update={
                    "signature": sign_ed25519(
                        unsigned_override.canonical_bytes(), private["coire-core"]
                    )
                }
            )
            node_app = FastAPI()
            node_app.include_router(node_failover_routes.router)
            async with AsyncClient(
                transport=ASGITransport(app=node_app), base_url="http://coire-edge-a"
            ) as node_client:
                delivered = await node_client.post(
                    "/node/failover/election/override", json=override.model_dump(mode="json")
                )
            assert delivered.status_code == 204
            participant = node_failover_routes.get_participant()
            assert participant is not None and participant.active_override() == override
            assert any(
                event.kind.value == "override_inhibit"
                for event in ElectionJournal(
                    tmp_path / "edge" / "coire-edge-a.journal.json"
                ).entries()
            )
            reconciled: list[str] = []

            @asynccontextmanager
            async def fake_session() -> AsyncIterator[object]:
                yield object()

            async def fake_reconcile(_session: object, events: list[FailoverEvent]) -> int:
                reconciled.extend(event.kind.value for event in events)
                return len(events)

            monkeypatch.setattr(core_failover_routes, "session_scope", fake_session)
            monkeypatch.setattr(core_failover_routes, "reconcile_events", fake_reconcile)
            assert await controller.refresh_once()
            assert reconciled == ["override_inhibit"]
            await controller.stop()
    finally:
        core_failover_routes.set_participant(None)


@pytest.mark.asyncio
async def test_signed_handback_route_fences_and_drains_a_promoted_studio(tmp_path: Path) -> None:
    private, members, membership = _members()
    now = datetime.now(UTC)
    timings = ElectionTimings(
        promotion=timedelta(seconds=10),
        demotion=timedelta(seconds=30),
        lease=timedelta(minutes=5),
        drain=timedelta(seconds=30),
    )
    edge = StudioParticipant(
        name="coire-edge-a",
        membership=membership,
        private_key_b64=private["coire-edge-a"],
        timings=timings,
        proof_path=tmp_path / "proof.json",
    )
    view = _view(now, frozenset({"coire-core"}))["coire-edge-a"]
    edge.observe(view, now)
    members["coire-edge-b"].observe(view, now)
    grant = members["coire-edge-b"].vote(1, edge.term, edge.name, now)
    assert grant is not None and edge.receive_grant(grant, now)
    assert edge.observe(view, now) is ServiceRole.ELECTED
    edge.in_flight = 1
    unsigned = HandbackNotice(
        epoch=1,
        term=edge.term,
        holder=edge.name,
        successor="coire-core",
        expires_at=now + timedelta(seconds=30),
        signature="unsigned",
    )
    notice = unsigned.model_copy(
        update={"signature": sign_ed25519(unsigned.canonical_bytes(), private["coire-core"])}
    )
    node_failover_routes.set_participant(edge)
    node_app = FastAPI()
    node_app.include_router(node_failover_routes.router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=node_app), base_url="http://coire-edge-a"
        ) as client:
            response = await client.post(
                "/node/failover/election/handback", json=notice.model_dump(mode="json")
            )
        assert response.status_code == 204
        assert edge.role is ServiceRole.DRAINING
        assert not edge.proof_path.exists()
        assert edge.observe(view, now + timedelta(seconds=1), in_flight=0) is ServiceRole.STANDBY
    finally:
        node_failover_routes.set_participant(None)


def test_ingress_fails_closed_and_the_frontend_is_a_separate_hardened_service() -> None:
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    balancer = yaml.safe_load((root / "deploy/cloudflared/load-balancer.yaml.tmpl").read_text())
    assert balancer["fallback_pool"] is None
    assert balancer["access_application"]
    monitors = {pool["name"]: pool["monitor"] for pool in balancer["pools"]}
    assert monitors["coire-core"]["path"] == "/failover/ready"
    assert monitors["coire-edge-a"]["path"] == "/ready"
    assert monitors["coire-edge-b"]["expected_code"] == 200
    assert all(pool["monitor"]["no_cache"] is True for pool in balancer["pools"])
    compose = (root / "deploy/compose/compose.yaml").read_text()
    assert 'profiles: ["studio-failover"]' in compose or "studio-failover" in compose
    assert "coire-failover" in compose
    assert "cap_drop: [ALL]" in compose
    nginx = (root / "apps/coire-web/nginx/nginx.conf").read_text()
    assert "location /failover/" in nginx
    assert "/nginx-health" in nginx
