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
    FailoverLease,
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
pytestmark = pytest.mark.integration


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
        lease=timedelta(seconds=15),
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
        target_host="coire-edge-b",
        epoch=1,
        issued_at=now,
        fencing_evidence="isolated other public origins fenced",
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
    # Renew the Studio lease during the simulated outage instead of attempting
    # to drain an already expired request after a thirty-second time jump.
    _settle(participants, now + timedelta(seconds=16), absent=frozenset({"coire-core"}))
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
    assert not core.serving(later), "the predecessor's outstanding grants must expire first"
    _settle(participants, later + timedelta(seconds=2))
    assert core.serving(later + timedelta(seconds=2))


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
        expires_at=now + timedelta(seconds=120),
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
                target_host="coire-edge-a",
                epoch=1,
                issued_at=now,
                fencing_evidence="isolated maintenance inhibition",
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
            assert delivered.status_code == 200
            assert delivered.json()["request_id"] == str(override.override_id)
            assert delivered.json()["host"] == "coire-edge-a"
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
            assert ElectionJournal(tmp_path / "edge" / "coire-edge-a.journal.json").entries() == []
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
        issued_at=now,
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
        assert response.status_code == 200
        acknowledgement = response.json()
        assert acknowledgement["request_id"] == str(notice.request_id)
        assert acknowledgement["host"] == edge.name and acknowledgement["accepted"] is True
        assert edge.role is ServiceRole.DRAINING
        retained = FailoverLease.model_validate_json(edge.proof_path.read_text())
        assert retained.phase == "draining" and retained.drain_deadline is not None
        assert retained.drain_deadline <= retained.authority_expires_at
        assert not edge.serving(now)
        assert edge.observe(view, now + timedelta(seconds=1), in_flight=0) is ServiceRole.STANDBY
        assert not edge.proof_path.exists()
    finally:
        node_failover_routes.set_participant(None)


def test_ingress_fails_closed_and_the_frontend_is_a_separate_hardened_service() -> None:
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    balancer = yaml.safe_load((root / "deploy/cloudflared/load-balancer.yaml.tmpl").read_text())
    policy = balancer["load_balancer"]
    assert policy["proxied"] is True
    assert policy["steering_policy"] == "off"
    assert policy["session_affinity"] == "none"
    assert policy["default_pools"] == [
        "${COIRE_CORE_POOL_ID}",
        "${COIRE_EDGE_A_POOL_ID}",
        "${COIRE_EDGE_B_POOL_ID}",
    ]
    assert policy["fallback_pool"] == "${COIRE_DISABLED_POOL_ID}"
    assert policy["fallback_pool"] not in policy["default_pools"]
    fallback = balancer["pools"]["coire-disabled-fallback"]
    assert fallback["enabled"] is False
    assert all(origin["enabled"] is False for origin in fallback["origins"])
    monitors = balancer["monitors"]
    for member, monitor in monitors.items():
        assert monitor["path"] == ("/failover/ready" if member == "coire-core" else "/ready")
        assert monitor["expected_codes"] == "200"
        assert monitor["type"] == "https"
        assert monitor["allow_insecure"] is False
        assert monitor["follow_redirects"] is False
        assert monitor["interval"] == 15
        assert monitor["timeout"] == 5
        assert monitor["retries"] == 0
        assert monitor["header"]["Host"] == ["${COIRE_PUBLIC_HOSTNAME}"]
        assert monitor["header"]["Cache-Control"] == ["no-cache, no-store"]
        assert len(monitor["header"]["X-Coire-Failover-Monitor"]) == 1
        pool = balancer["pools"][member]
        assert pool["enabled"] is True
        assert pool["minimum_origins"] == 1
        assert pool["origins"][0]["address"].endswith(".cfargotunnel.com")
    compose = (root / "deploy/compose/compose.yaml").read_text()
    assert 'profiles: ["studio-failover"]' in compose or "studio-failover" in compose
    assert "coire-failover" in compose
    assert "cap_drop: [ALL]" in compose
    nginx = (root / "apps/coire-web/nginx/nginx.conf").read_text()
    assert "location /failover/" in nginx
    assert "/nginx-health" in nginx


async def test_full_core_middleware_preserves_safety_exchanges_during_019_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the full app stack; the SQL fence observation is an isolated store seam."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from coire_api import auth
    from coire_api.app import create_app as core_app
    from coire_api.auth import Principal, PrincipalKind
    from coire_api.upgrades import admission
    from coire_core.failover_crypto import verify_ed25519
    from coire_core.models.failover import FailoverHeartbeat

    private, participants, membership = _members()
    now = datetime.now(UTC)
    core = CoreParticipant(
        name="coire-core",
        membership=membership,
        private_key_b64=private["coire-core"],
        timings=participants["coire-core"].timings,
    )
    participants["coire-core"] = core
    _settle(participants, now)
    assert core.serving()
    settings = Settings(
        _secrets_dir="/nonexistent",
        upgrades_enabled=True,
        failover_member_name="coire-core",
        failover_peer_key=SecretStr(private["coire-core"]),
    )
    app = core_app(settings)
    reached: list[str] = []

    @app.get("/v1/qualification")
    async def guarded_operation() -> dict[str, bool]:
        reached.append("admitted")
        return {"ok": True}

    owner = {"job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "generation": 9, "released": False}
    preimage = dict(owner)
    reads: list[str] = []

    async def fence_query(_statement: object) -> str | None:
        reads.append("fence")
        return None if owner["released"] else "coire-api"

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(scalar=fence_query)

    monkeypatch.setattr(admission, "session_scope", session)
    monkeypatch.setattr(
        auth,
        "authenticate_request",
        AsyncMock(return_value=Principal(kind=PrincipalKind.USER, user_id=uuid4())),
    )
    core_failover_routes.set_participant(core)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://core") as client:
            assert (await client.get("/v1/qualification")).status_code == 503
            assert not reached
            before = len(reads)
            heartbeat = participants["coire-edge-a"].issue_heartbeat(datetime.now(UTC))
            result = await client.post(
                "/api/v1/failover/heartbeat", json=heartbeat.model_dump(mode="json")
            )
            assert result.status_code == 200
            reply = FailoverHeartbeat.model_validate(result.json())
            assert reply.challenge == heartbeat.challenge
            assert verify_ed25519(
                reply.canonical_bytes(), reply.signature, core.member_public_key(core.name)
            )
            assert len(reads) == before, "signed non-DB safety exchange must survive maintenance"
            assert (await client.post("/api/v1/failover/heartbeat", json={})).status_code == 401
            # DB-backed snapshot/event work retains the original maintenance fence.
            assert (
                await client.post(
                    "/api/v1/failover/snapshot", json=heartbeat.model_dump(mode="json")
                )
            ).status_code == 503
            assert (await client.post("/api/v1/failover/events", json={})).status_code == 503
            assert owner == preimage
            assert not (await app.state.upgrade_admissions.inventory(("coire-api",))).entries
            # 019 alone releases ownership; failover never mutates it. Both successful
            # activation and verified rollback use this same released-fence observation.
            owner["released"] = True
            assert (await client.get("/v1/qualification")).status_code == 200
            core._fence(outage=True)
            assert (await client.get("/v1/qualification")).status_code == 503
            assert reached == ["admitted"], "a stale Core route must never reach user work"
            assert owner == {**preimage, "released": True}
    finally:
        core_failover_routes.set_participant(None)


async def test_signed_peer_liveness_preserves_sql_owner_and_inflight_stream(
    training_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real SQL fence acquisition and full middleware; no Docker transition is simulated."""
    import asyncio

    from fastapi.responses import StreamingResponse
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from upgrade_failover_fixtures import ArmedCoreProbe
    from upgrade_fixtures import core_plan

    from coire_api.auth import Principal, PrincipalKind
    from coire_api.db import Base, UpgradeMaintenanceRow, UpgradeOperationRow, UserRow
    from coire_api.upgrades import admission
    from coire_api.upgrades.persistence import accept, register_release
    from coire_core.models.auth import UserRole
    from coire_core.models.upgrades import UpgradeStartRequest
    from coire_scheduler.maintenance import fence

    schema = f"failover_upgrade_race_{uuid4().hex}"
    owner_engine = create_async_engine(training_postgres_url)
    async with owner_engine.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    probe = ArmedCoreProbe(monkeypatch)
    # Replace the helper's explicit unit-test SQL seam with actual committed transactions.
    monkeypatch.setattr(admission, "session_scope", sessions.begin)
    probe.fenced = False
    started, finish = asyncio.Event(), asyncio.Event()
    request = None

    @probe.app.get("/v1/qualification-stream")
    async def stream() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            started.set()
            yield b"before-maintenance\n"
            await finish.wait()
            yield b"completed-after-maintenance\n"

        return StreamingResponse(body())

    async def retained_owner() -> list[dict[str, object]]:
        async with sessions.begin() as session:
            rows = (await session.scalars(select(UpgradeMaintenanceRow))).all()
            operation = await session.scalar(select(UpgradeOperationRow))
            assert operation is not None
            return [
                {"operation": operation.payload, "active_slot": operation.active_slot},
                *[
                    {
                        "target": row.target,
                        "job_id": row.job_id,
                        "generation": row.generation,
                        "released": row.released,
                        "payload": row.payload,
                    }
                    for row in sorted(rows, key=lambda value: value.target)
                ],
            ]

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text("CREATE SEQUENCE upgrade_generation_seq"))
        plan = core_plan()
        owner = plan.source.prepared_by
        principal = Principal(kind=PrincipalKind.ADMIN, user_id=owner, role=UserRole.ADMIN)
        manifest = plan.target.manifest.model_copy(
            update={
                "services": (
                    plan.target.manifest.services[0].model_copy(
                        update={"image_id": "sha256:" + "b" * 64}
                    ),
                )
            }
        )
        target = plan.target.model_copy(
            update={"manifest": manifest, "manifest_sha256": manifest.sha256()}
        )
        async with sessions.begin() as session:
            session.add(
                UserRow(
                    id=owner,
                    email=f"{owner}@fixture.test",
                    display_name="Admin",
                    role=UserRole.ADMIN,
                )
            )
        async with sessions.begin() as session:
            await register_release(session, principal, plan.source)
            await register_release(session, principal, target)
            operation, created = await accept(
                session,
                principal,
                UpgradeStartRequest(
                    release_id=target.release_id,
                    expected_source_sha256=plan.source.manifest_sha256,
                    idempotency_key=uuid4(),
                    acknowledge_maintenance=True,
                ),
                plan.source.release_id,
            )
            assert created
        await probe.probe(tmp_path)
        request = asyncio.create_task(probe.client.get("/v1/qualification-stream"))
        await asyncio.wait_for(started.wait(), timeout=2)
        async with sessions.begin() as session:
            await fence(
                session,
                plan.maintenance.model_copy(
                    update={"job_id": operation.job_id, "generation": operation.generation}
                ),
            )
        probe.fenced = True
        before = await retained_owner()
        assert len(before) == 5 and all(not row["released"] for row in before[1:])
        for _ in range(3):
            await probe.probe(tmp_path, expect_inflight=True)
            assert await retained_owner() == before
        assert (await probe.client.get("/v1/qualification-stream")).status_code == 503
        finish.set()
        completed = await asyncio.wait_for(request, timeout=2)
        assert completed.status_code == 200
        assert completed.content == b"before-maintenance\ncompleted-after-maintenance\n"
        await probe.probe(tmp_path)
        assert await retained_owner() == before
        assert probe.reached == 1
    finally:
        finish.set()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        await probe.close()
        await engine.dispose()
        async with owner_engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await owner_engine.dispose()
