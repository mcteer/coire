"""Offline native gate through authenticated frontend and node routes; never runs on Core."""

from __future__ import annotations

import base64
import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_core.failover_crypto import public_key_b64, sign_ed25519
from coire_core.failover_election import (
    ElectionTimings,
    MemberHealth,
    MemberObservation,
    ServiceRole,
)
from coire_core.failover_residency import single_placement
from coire_core.models.adapters import InferenceTarget
from coire_core.models.engine import EngineState
from coire_core.models.failover import (
    ElectionSafetyRecord,
    ElectionVoteGrant,
    FailoverAccessSubject,
    FailoverAccessVerifier,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverModel,
    FailoverRelayAuthority,
    FailoverRelayIssuance,
    FailoverRelayRequest,
    FailoverResidentEngine,
    FailoverSnapshot,
)
from coire_core.settings import Settings
from coire_failover.app import FailoverRuntime, create_app
from coire_node.failover.participant import StudioParticipant
from coire_node.routes import failover as node_routes
from coire_node.testing.harness import TOKEN, Agent
from coire_node.testing.training import offline_training_model

pytestmark = [pytest.mark.integration, pytest.mark.engine]


def test_real_resident_failover_completion_and_stale_process_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.environ.get("COIRE_FAILOVER_ENGINE") != "1":
        pytest.skip("opt in with COIRE_FAILOVER_ENGINE=1 on an isolated Apple Silicon worker")
    # Validates host, <=1 GiB, acquired manifest and every byte before any native import/work.
    source = offline_training_model(os.environ.get("COIRE_TEST_MODEL"))
    monkeypatch.delenv("COIRE_ENGINE_COMMAND", raising=False)
    monkeypatch.delenv("COIRE_TEST_FAKE_VALIDATION", raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    agent = Agent(tmp_path / "node", node_name="coire-edge-a", node_engine_port_range="19500-19509")
    shutil.copytree(
        source,
        agent.store.path_for(source.name),
        copy_function=os.link,
        ignore=shutil.ignore_patterns(".cache"),
    )
    shutil.copyfile(
        source.with_name(source.name + ".manifest.json"), agent.store.manifest_path(source.name)
    )
    manifest = agent.store.read_manifest(source.name)
    assert manifest is not None
    target = InferenceTarget(
        model_id=uuid4(), variant_id=uuid4(), base_manifest_sha256=manifest.sha256()
    )
    engine_id = uuid4()
    app = agent.app()
    try:
        with TestClient(app) as node:
            loaded = node.post(
                "/node/engines",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={
                    "engine_id": str(engine_id),
                    "slug": source.name,
                    "estimate_bytes": 1024**3,
                    "target": target.model_dump(mode="json"),
                },
            )
            assert loaded.status_code == 202, loaded.text
            deadline = time.monotonic() + 120
            while True:
                engine = agent.engines.get(engine_id)
                assert engine is not None
                assert engine.state is not EngineState.FAILED, engine.state_reason
                if engine.state is EngineState.READY:
                    break
                assert time.monotonic() < deadline, "tiny engine did not become ready"
                time.sleep(0.1)
        placement = single_placement(uuid4(), "coire-edge-a", engine)
        assert placement is not None
        names = ("coire-core", "coire-edge-a", "coire-edge-b")
        private = {
            name: base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()
            for name in names
        }
        membership = FailoverMembershipConfig(
            epoch=1,
            signing_key_id="isolated-native",
            members=[
                FailoverMember(name=name, priority=i, public_key=public_key_b64(private[name]))
                for i, name in enumerate(names)
            ],
        )
        now = datetime.now(UTC)
        safety = tmp_path / "safety.json"
        safety.write_text(
            ElectionSafetyRecord(
                member=names[1], epoch=1, highest_term=1, prior_max_lifetime_s=15
            ).model_dump_json()
        )
        participant = StudioParticipant(
            name=names[1],
            membership=membership,
            private_key_b64=private[names[1]],
            proof_path=tmp_path / "proof.json",
            safety_path=safety,
            timings=ElectionTimings(
                promotion=timedelta(seconds=15),
                demotion=timedelta(seconds=45),
                lease=timedelta(seconds=15),
                drain=timedelta(seconds=30),
            ),
        )
        observations = [
            MemberObservation(
                name=name,
                health=MemberHealth.UNREACHABLE if name == names[0] else MemberHealth.HEALTHY,
                since=now - timedelta(seconds=20),
            )
            for name in names
        ]
        participant.observe(observations, now)
        grant = ElectionVoteGrant(
            epoch=1,
            term=participant.term,
            candidate=names[1],
            voter=names[2],
            issued_at=now,
            expires_at=now + timedelta(seconds=15),
            signature="unsigned",
        )
        participant.receive_grant(
            grant.model_copy(
                update={"signature": sign_ed25519(grant.canonical_bytes(), private[names[2]])}
            ),
            now,
        )
        assert participant.observe(observations, now) is ServiceRole.ELECTED
        model = FailoverModel(
            id=target.model_id,
            slug="native-tiny",
            display_name="Tiny",
            context_window=2048,
            capabilities=frozenset({"text_chat"}),
            placement=placement,
        )
        snapshot = FailoverSnapshot(
            snapshot_id=uuid4(),
            issued_at=now,
            expires_at=now + timedelta(seconds=120),
            membership=membership,
            models=[model],
            subjects=[
                FailoverAccessSubject(
                    issuer="https://isolated.fixture",
                    subject="operator",
                    eligible=True,
                    policy_version=1,
                )
            ],
            access_verifier=FailoverAccessVerifier(
                issuer="https://isolated.fixture",
                audience="native-test",
                jwks_url="https://isolated.fixture/keys",
            ),
            signature="unsigned",
        )
        snapshot = snapshot.model_copy(
            update={"signature": sign_ed25519(snapshot.canonical_bytes(), private[names[0]])}
        )
        path = tmp_path / "snapshot.json"
        path.write_text(snapshot.model_dump_json())
        settings = agent.settings
        settings.failover_member_name = names[1]
        settings.failover_snapshot_path = str(path)
        settings.failover_core_public_key = public_key_b64(private[names[0]])
        settings.failover_peer_key = SecretStr(private[names[1]])
        settings.failover_relay_token = SecretStr("isolated-native-relay")
        node_routes.set_participant(participant)
        signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing.public_key(), as_dict=True)
        jwk.update(kid="test", alg="RS256", use="sig")
        access = httpx.AsyncClient(
            mounts={
                "https://isolated.fixture": httpx.MockTransport(
                    lambda _: httpx.Response(200, json={"keys": [jwk]})
                )
            }
        )
        monkeypatch.setattr(node_routes, "_http_client", lambda: access)
        token = jwt.encode(
            {
                "iss": "https://isolated.fixture",
                "aud": "native-test",
                "sub": "operator",
                "email": "operator@example.test",
                "iat": int(now.timestamp()),
                "exp": int(now.timestamp()) + 60,
            },
            signing,
            algorithm="RS256",
            headers={"kid": "test"},
        )
        transport = httpx.ASGITransport(app=app)

        async def resident() -> list[FailoverResidentEngine]:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://isolated-node"
            ) as client:
                response = await client.get(
                    "/node/failover/resident",
                    headers={"X-Coire-Failover-Relay": "isolated-native-relay"},
                )
                response.raise_for_status()
                return [FailoverResidentEngine.model_validate(item) for item in response.json()]

        async def issue(body: FailoverRelayIssuance) -> FailoverRelayAuthority:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://isolated-node"
            ) as client:
                response = await client.post(
                    "/node/failover/authority",
                    headers={"X-Coire-Failover-Relay": "isolated-native-relay"},
                    json=body.model_dump(mode="json"),
                )
                response.raise_for_status()
                return FailoverRelayAuthority.model_validate_json(response.content)

        async def relay(body: FailoverRelayRequest, destination: str):  # type: ignore[no-untyped-def]
            from fastapi import Response

            assert destination == "local"
            async with httpx.AsyncClient(
                transport=transport, base_url="http://isolated-node"
            ) as client:
                response = await client.post(
                    f"/node/failover/engines/{engine_id}/chat/completions",
                    headers={"X-Coire-Failover-Relay": "isolated-native-relay"},
                    json=body.model_dump(mode="json"),
                )
                return Response(
                    response.content,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                )

        runtime = FailoverRuntime(
            settings=Settings(_secrets_dir="/nonexistent", failover_member_name=names[1]),  # type: ignore[call-arg]
            load_snapshot=lambda: snapshot,
            load_lease=participant.lease,
            local_resident=resident,
            issue_authority=issue,
            relay=relay,
            access_client=access,
        )
        with TestClient(create_app(runtime)) as frontend:
            headers = {"Cf-Access-Jwt-Assertion": token}
            payload = {
                "model": str(model.id),
                "messages": [{"role": "user", "content": "Say hello."}],
                "max_tokens": 8,
            }
            assert frontend.post("/v1/chat/completions", json=payload).status_code == 401
            for stream in (False, True):
                result = frontend.post(
                    "/v1/chat/completions", json={**payload, "stream": stream}, headers=headers
                )
                assert result.status_code == 200, result.text
                assert (
                    ("[DONE]" in result.text)
                    if stream
                    else bool(result.json()["choices"][0]["message"]["content"])
                )
            # Same slug/target after process replacement is not an authorised generation.
            owned = agent.engines._engines[str(engine_id)]
            created = owned.create_time
            owned.create_time = (created or 0) + 1
            try:
                refused = frontend.post("/v1/chat/completions", json=payload, headers=headers)
                assert (
                    refused.status_code == 503 and refused.json()["reason"] == "model_not_resident"
                )
            finally:
                owned.create_time = created
            assert len(agent.engines.statuses()) == 1
    finally:
        node_routes.set_participant(None)
        for owned_engine in agent.engines.statuses():
            if owned_engine.engine_id is not None:
                agent.engines.stop(owned_engine.engine_id)
        agent.close()
