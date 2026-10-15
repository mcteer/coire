from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_core.failover_authority import create_relay_authority
from coire_core.failover_crypto import sign_ed25519, verify_ed25519
from coire_core.failover_election import (
    ElectionTimings,
    MemberHealth,
    MemberObservation,
    ServiceRole,
)
from coire_core.failover_limits import bounded_text_request
from coire_core.failover_residency import single_placement
from coire_core.models.adapters import InferenceTarget
from coire_core.models.engine import EngineState, EngineStatus
from coire_core.models.failover import (
    ElectionSafetyRecord,
    ElectionVoteGrant,
    FailoverAccessSubject,
    FailoverAccessVerifier,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverModel,
    FailoverRelayAuthority,
    FailoverSinglePlacement,
    FailoverSnapshot,
)
from coire_core.models.gateway import ChatCompletionRequest, ChatMessage
from coire_core.models.node import NodePath
from coire_node.failover.participant import StudioParticipant
from coire_node.routes import failover as routes
from coire_node.testing.harness import TOKEN, Agent


@pytest.mark.parametrize(
    "fault", [None, "pid", "artifact", "stopped", "replaced_during_identity", "maintenance_stream"]
)
def test_native_relay_rechecks_exact_process_before_engine_work(
    agent: Agent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    import httpx

    participant, snapshot, private = signing_peer(agent, tmp_path)
    routes.set_participant(participant)
    model = snapshot.models[0]
    assert model.placement is not None
    slug = "fixture--tiny"
    agent.store.path_for(slug).mkdir(parents=True)
    (agent.store.path_for(slug) / "config.json").write_text("{}")
    manifest = agent.store.hash_tree(slug, repo_id="fixture/tiny", revision="offline")
    agent.store.write_manifest(manifest)
    engine = EngineStatus(
        engine_id=model.placement.engine_id,
        slug=slug,
        pid=123,
        process_create_time=1000,
        port=9500,
        state=EngineState.READY,
        started_at=datetime.now(UTC),
        target=model.placement.target.model_copy(
            update={"base_manifest_sha256": manifest.sha256()}
        ),
    )
    placement = single_placement(model.placement.instance_id, "coire-edge-a", engine)
    assert placement is not None
    model = model.model_copy(update={"placement": placement})
    snapshot = snapshot.model_copy(update={"models": [model]})
    snapshot = snapshot.model_copy(
        update={"signature": sign_ed25519(snapshot.canonical_bytes(), private["coire-core"])}
    )
    Path(agent.settings.failover_snapshot_path).write_text(snapshot.model_dump_json())
    observed = engine
    monkeypatch.setattr(agent.engines, "failover_status", lambda _: observed)

    async def identity(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal observed
        if fault == "replaced_during_identity":
            observed = engine.model_copy(update={"process_create_time": 1001})
        return {
            "sub": "approved",
            "iss": "https://access.fixture",
            "exp": (datetime.now(UTC) + timedelta(seconds=30)).timestamp(),
        }

    monkeypatch.setattr(routes, "verify_access_assertion", identity)
    from coire_node.maintenance import NodeMaintenance

    node_app = agent.app(NodePath.MESH)
    gate = NodeMaintenance(tmp_path / "maintenance", "coire-edge-a")
    node_app.state.maintenance = gate
    body = bounded_text_request(
        ChatCompletionRequest(
            model=model.id,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=8,
            stream=fault == "maintenance_stream",
        ),
        model,
        snapshot.subjects[0].limits,
    )
    lease = participant.lease()
    assert lease is not None
    authority = create_relay_authority(
        lease=lease,
        snapshot=snapshot,
        engine_id=placement.engine_id,
        request=body,
        subject="approved",
        identity_expiry=datetime.now(UTC) + timedelta(seconds=30),
        private_key=private[participant.name],
    )
    seen: list[str] = []

    class DuringMaintenance(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            from coire_core.models.upgrades import NodeMaintenanceCommand, UpgradeMaintenance

            yield b'data: {"choices":[]}\n\n'
            now = datetime.now(UTC)
            maintenance = UpgradeMaintenance(
                job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
                generation=7,
                targets=("coire-edge-a",),
                state="draining",
                updated_at=now,
                drain_deadline_at=now + timedelta(seconds=30),
            )
            gate.apply(NodeMaintenanceCommand(action="apply", maintenance=maintenance))
            inventory = gate.inventory(maintenance.job_id, 7, "a" * 64)
            assert next(item for item in inventory.owners if item.owner == "requests").entries
            assert gate.current() == maintenance
            yield b"data: [DONE]\n\n"

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if fault == "maintenance_stream":
            return httpx.Response(200, stream=DuringMaintenance())
        return httpx.Response(200, json={"choices": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    monkeypatch.setattr(routes, "_http_client", lambda: client)
    if fault == "pid":
        observed = engine.model_copy(update={"pid": 124})
    elif fault == "artifact":
        assert engine.target is not None
        observed = engine.model_copy(
            update={"target": engine.target.model_copy(update={"base_manifest_sha256": "c" * 64})}
        )
    elif fault == "stopped":
        observed = engine.model_copy(update={"state": EngineState.STOPPED})
    try:
        with TestClient(node_app) as http:
            result = http.post(
                f"/node/failover/engines/{placement.engine_id}/chat/completions",
                headers={"X-Coire-Failover-Relay": "relay-only-token"},
                json={
                    "engine_id": str(placement.engine_id),
                    "model_slug": model.slug,
                    "request": body.model_dump(mode="json"),
                    "authority": authority.model_dump(mode="json"),
                    "access_assertion": "isolated-verified-identity",
                },
            )
            if fault == "maintenance_stream":
                assert "[DONE]" in result.text
                assert gate._admitting == 0
                assert gate.current().generation == 7
                refused = http.post(
                    "/node/failover/authority",
                    headers={"X-Coire-Failover-Relay": "relay-only-token"},
                    json={
                        "engine_id": str(placement.engine_id),
                        "request": body.model_dump(mode="json"),
                        "access_assertion": "isolated-verified-identity",
                    },
                )
                assert refused.status_code == 503
        successful = fault in (None, "maintenance_stream")
        assert result.status_code == (200 if successful else 503), result.text
        assert len(seen) == (1 if successful else 0)
    finally:
        routes.set_participant(None)


def test_failover_relay_uses_its_own_scoped_credential(agent: Agent) -> None:
    agent.settings.failover_relay_token = SecretStr("relay-only-token")
    with TestClient(agent.app(NodePath.MESH)) as client:
        # The broad node registration credential cannot access failover relay operations.
        response = client.get(
            "/node/failover/resident", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 401
        response = client.get(
            "/node/failover/resident", headers={"X-Coire-Failover-Relay": "relay-only-token"}
        )
    assert response.status_code == 200
    assert response.json() == []


def test_fallback_listener_does_not_expose_failover_relay(agent: Agent) -> None:
    with TestClient(agent.app(NodePath.FALLBACK)) as client:
        response = client.get(
            "/node/failover/resident",
            headers={"X-Coire-Path": "fallback", "X-Coire-Failover-Relay": "relay-only-token"},
        )
    assert response.status_code == 404


def signing_peer(
    agent: Agent, tmp_path: Path
) -> tuple[StudioParticipant, FailoverSnapshot, dict[str, str]]:
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
    safety_path = tmp_path / "trusted.safety.json"
    safety_path.write_text(
        ElectionSafetyRecord(
            member="coire-edge-a", epoch=1, highest_term=1, prior_max_lifetime_s=15
        ).model_dump_json()
    )
    participant = StudioParticipant(
        name="coire-edge-a",
        membership=membership,
        private_key_b64=private["coire-edge-a"],
        proof_path=tmp_path / "proof.json",
        safety_path=safety_path,
        timings=ElectionTimings(
            promotion=timedelta(seconds=15),
            demotion=timedelta(seconds=45),
            lease=timedelta(seconds=15),
            drain=timedelta(seconds=30),
        ),
    )
    now = datetime.now(UTC)
    observations = [
        MemberObservation(
            name=name,
            health=MemberHealth.UNREACHABLE if name == "coire-core" else MemberHealth.HEALTHY,
            since=now - timedelta(seconds=20),
        )
        for name in private
    ]
    participant.observe(observations, now)
    grant = ElectionVoteGrant(
        epoch=1,
        term=participant.term,
        candidate=participant.name,
        voter="coire-edge-b",
        issued_at=now,
        expires_at=now + timedelta(seconds=15),
        signature="unsigned",
    )
    grant = grant.model_copy(
        update={"signature": sign_ed25519(grant.canonical_bytes(), private["coire-edge-b"])}
    )
    assert participant.receive_grant(grant, now)
    assert participant.observe(observations, now) is ServiceRole.ELECTED
    model_id = uuid4()
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
                    engine_id=uuid4(),
                    node="coire-edge-a",
                    generation="a" * 64,
                    target=InferenceTarget(
                        model_id=model_id, variant_id=uuid4(), base_manifest_sha256="b" * 64
                    ),
                ),
            )
        ],
        subjects=[
            FailoverAccessSubject(
                issuer="https://access.fixture", subject="approved", eligible=True, policy_version=1
            )
        ],
        access_verifier=FailoverAccessVerifier(
            issuer="https://access.fixture", audience="aud", jwks_url="https://access.fixture/keys"
        ),
        signature="unsigned",
    )
    snapshot = snapshot.model_copy(
        update={"signature": sign_ed25519(snapshot.canonical_bytes(), private["coire-core"])}
    )
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(snapshot.model_dump_json())
    agent.settings.failover_member_name = participant.name
    agent.settings.node_name = participant.name
    agent.settings.failover_peer_key = SecretStr(private[participant.name])
    agent.settings.failover_core_public_key = members[0].public_key
    agent.settings.failover_snapshot_path = str(snapshot_path)
    agent.settings.failover_relay_token = SecretStr("relay-only-token")
    return participant, snapshot, private


@pytest.mark.parametrize("revoked_during_identity", [False, True])
def test_node_owned_signing_rechecks_authority_after_identity_io(
    agent: Agent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revoked_during_identity: bool,
) -> None:
    participant, snapshot, _ = signing_peer(agent, tmp_path)
    routes.set_participant(participant)

    async def identity(*_args: object, **_kwargs: object) -> dict[str, object]:
        if revoked_during_identity:
            participant.role = ServiceRole.STANDBY
            participant.proof = None
        return {
            "sub": "approved",
            "iss": "https://access.fixture",
            "exp": (datetime.now(UTC) + timedelta(seconds=30)).timestamp(),
        }

    monkeypatch.setattr(routes, "verify_access_assertion", identity)
    assert snapshot.models[0].placement is not None
    body = {
        "engine_id": str(snapshot.models[0].placement.engine_id),
        "access_assertion": "isolated-verified-identity-fixture",
        "request": {
            "model": str(snapshot.models[0].id),
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
            "coire_wait_for_model": False,
        },
    }
    try:
        with TestClient(agent.app(NodePath.MESH)) as client:
            denied = client.post(
                "/node/failover/authority", json=body, headers={"Authorization": f"Bearer {TOKEN}"}
            )
            assert denied.status_code == 401
            response = client.post(
                "/node/failover/authority",
                json=body,
                headers={"X-Coire-Failover-Relay": "relay-only-token"},
            )
        if revoked_during_identity:
            assert response.status_code == 503
        else:
            assert response.status_code == 200
            authority = FailoverRelayAuthority.model_validate(response.json())
            assert authority.holder == participant.name and authority.identity_subject == "approved"
            assert str(authority.engine_id) == body["engine_id"]
            public = next(
                member.public_key
                for member in snapshot.membership.members
                if member.name == participant.name
            )
            assert verify_ed25519(authority.canonical_bytes(), authority.signature, public)
    finally:
        routes.set_participant(None)


def test_scoped_node_body_is_bounded_before_validation(agent: Agent) -> None:
    agent.settings.failover_relay_token = SecretStr("relay-only-token")
    with TestClient(agent.app(NodePath.MESH)) as client:
        denied = client.post("/node/failover/authority", content=b"x" * 65537)
        large = client.post(
            "/node/failover/authority",
            content=b"x" * 65537,
            headers={"X-Coire-Failover-Relay": "relay-only-token"},
        )
    assert denied.status_code == 401
    assert large.status_code == 413


def test_election_vote_without_a_participant_fails_closed(agent: Agent) -> None:
    with TestClient(agent.app(NodePath.MESH)) as client:
        response = client.post("/node/failover/election/votes", json={})
    assert response.status_code == 401


def test_generated_node_schema_declares_signed_residency_and_authority(agent: Agent) -> None:
    schema = agent.app(NodePath.MESH).openapi()
    paths = schema["paths"]
    for route, request_model, response_model in (
        ("rank", "FailoverRankProbe", "FailoverRankObservation"),
        ("authority", "FailoverRelayIssuance", "FailoverRelayAuthority"),
    ):
        operation = paths[f"/node/failover/{route}"]["post"]
        assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
            request_model
        )
        assert operation["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith(response_model)
    models = schema["components"]["schemas"]
    assert models["FailoverRankObservation"]["additionalProperties"] is False
    assert {"challenge", "group_generation", "rank", "target", "signature"} <= set(
        models["FailoverRankObservation"]["required"]
    )
