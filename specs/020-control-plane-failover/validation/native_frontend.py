"""Operator-only native inference through the real hardened frontend image.

Requires explicit approval of temporary control listeners and the normal outbound
frontend network. Uses a local TLS identity fixture, not Cloudflare. The signed
membership and quorum are isolated fixtures; election timing is qualified separately.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import time
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import jwt
import psutil
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from frontend_container import FixtureDocker, FrontendContainer
from pydantic import SecretStr

from coire_core.failover_crypto import public_key_b64, sign_ed25519
from coire_core.failover_election import MemberHealth, MemberObservation, timings_from_settings
from coire_core.failover_poller import FailoverPoller
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
    FailoverSnapshot,
)
from coire_core.models.node import NetworkPath
from coire_node.failover.participant import StudioParticipant
from coire_node.failover.poller import StudioFailoverController
from coire_node.routes import failover as node_routes
from coire_node.testing.harness import Agent
from coire_node.testing.training import offline_training_model

HOST = "coire-edge-a.lab"
NODE_PORT, IDENTITY_PORT = 19440, 19441


class NativeDocker(FixtureDocker):
    """Approved fixture network, TLS trust and real node relay endpoints."""

    def __init__(self, root: Path) -> None:
        super().__init__(root.name + "-frontend")
        self.root = root

    async def create_network(self, name: str, *, internal: bool = True) -> str:
        # This is why the runner requires a separate, explicit network approval.
        return await super().create_network(name, internal=False)

    async def create_container(self, name: str, payload: Mapping[str, Any]) -> str:
        payload["Env"].extend(
            [
                f"FAILOVER_LOCAL_RELAY_URL=http://{HOST}:{NODE_PORT}",
                "SSL_CERT_FILE=/run/secrets/qualification-ca.pem",
            ]
        )
        payload["HostConfig"]["Binds"].append(
            f"{self.root / 'ca.pem'}:/run/secrets/qualification-ca.pem:ro"
        )
        return await super().create_container(name, payload)


def certificate(root: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    (root / "ca.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private = root.with_name(root.name + "-secrets")
    private.mkdir(mode=0o700)
    path = private / "tls-key.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)


async def main(args: argparse.Namespace) -> None:
    assert args.approve_temporary_control_listeners, "explicit network approval is required"
    assert socket.gethostname().split(".")[0] == "coire-edge-a", "never run engines on Core"
    expected: dict[str, str] = json.loads(
        await asyncio.to_thread(Path(args.expected_source_hashes).read_text)
    )
    assert expected and all(
        not Path(name).is_absolute() and ".." not in Path(name).parts for name in expected
    )
    identity_hashes = await asyncio.to_thread(
        lambda: {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in expected}
    )
    fixture_hash = hashlib.sha256(await asyncio.to_thread(Path(__file__).read_bytes)).hexdigest()
    assert identity_hashes == expected, "qualification checkout differs from selected source"
    address = socket.gethostbyname(HOST)
    assert ipaddress.ip_address(address).is_private and address == "192.168.4.11"
    source = offline_training_model(args.model)
    assert not os.environ.get("COIRE_ENGINE_COMMAND")
    assert not os.environ.get("COIRE_TEST_FAKE_VALIDATION")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    root = Path("/tmp/coire-020-native-" + uuid4().hex[:10])
    await asyncio.to_thread(root.mkdir, mode=0o700)
    frontend_root = root / (root.name + "-state")
    await asyncio.to_thread(frontend_root.mkdir, mode=0o755)
    await asyncio.to_thread(certificate, root)
    agent = Agent(
        root / "agent",
        node_name="coire-edge-a",
        node_engine_port_range="19450-19459",
        node_engine_start_timeout_s=120,
    )
    agent.settings.node_token = SecretStr(secrets.token_urlsafe(32))
    shutil.copytree(
        source,
        agent.store.path_for(source.name),
        copy_function=shutil.copy2,
        ignore=shutil.ignore_patterns(".cache"),
    )
    shutil.copyfile(
        source.with_name(source.name + ".manifest.json"), agent.store.manifest_path(source.name)
    )
    manifest = agent.store.read_manifest(source.name)
    assert manifest is not None
    for entry in manifest.files:
        copied = agent.store.path_for(source.name) / entry.path
        assert copied.stat().st_nlink == 1 and not copied.samefile(source / entry.path)
    settings = agent.settings
    settings.failover_member_name = "coire-edge-a"
    settings.failover_snapshot_path = str(frontend_root / "snapshot.json")
    settings.failover_proof_path = str(frontend_root / "proof.json")
    settings.failover_safety_path = str(root / "safety.json")
    names = ("coire-core", "coire-edge-a", "coire-edge-b")
    keys = {
        name: base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()
        for name in names
    }
    membership = FailoverMembershipConfig(
        epoch=1,
        signing_key_id="native-container-qualification",
        members=[
            FailoverMember(name=name, priority=i, public_key=public_key_b64(keys[name]))
            for i, name in enumerate(names)
        ],
    )
    settings.failover_peer_key = SecretStr(keys[names[1]])
    settings.failover_core_public_key = public_key_b64(keys[names[0]])
    frontend = FrontendContainer(frontend_root, settings, args.image)
    await frontend.docker.close()
    frontend.docker = NativeDocker(root)
    servers, tasks = [], []
    controller = None
    renew = None
    access = None
    result: dict[str, object] | None = None
    engine_id = uuid4()
    target = InferenceTarget(
        model_id=uuid4(), variant_id=uuid4(), base_manifest_sha256=manifest.sha256()
    )
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing.public_key(), as_dict=True)
    jwk.update(kid="qualification", alg="RS256", use="sig")
    identity = FastAPI(docs_url=None, openapi_url=None)

    @identity.get("/keys")
    async def jwks() -> dict[str, list[dict[str, Any]]]:
        return {"keys": [jwk]}

    async def serve(
        app: FastAPI,
        port: int,
        *,
        ssl_certfile: str | None = None,
        ssl_keyfile: str | None = None,
    ) -> None:
        ready = asyncio.Event()

        class Server(uvicorn.Server):
            async def startup(self, sockets: list[socket.socket] | None = None) -> None:
                await super().startup(sockets)
                ready.set()

        server = Server(
            uvicorn.Config(
                app,
                host=address,
                port=port,
                lifespan="off",
                log_level="critical",
                ssl_certfile=ssl_certfile,
                ssl_keyfile=ssl_keyfile,
            )
        )
        servers.append(server)
        tasks.append(asyncio.create_task(server.serve()))
        await asyncio.wait_for(ready.wait(), 10)
        assert server.started

    try:
        await frontend.prepare()
        container = frontend.container
        assert container is not None
        await serve(
            identity,
            IDENTITY_PORT,
            ssl_certfile=str(root / "ca.pem"),
            ssl_keyfile=str(root.with_name(root.name + "-secrets") / "tls-key.pem"),
        )
        access = httpx.AsyncClient(
            verify=ssl.create_default_context(cafile=str(root / "ca.pem")), trust_env=False
        )
        node_routes._client = access
        await serve(agent.app(NetworkPath.CONTROL), NODE_PORT)
        async with httpx.AsyncClient(
            base_url=f"http://{HOST}:{NODE_PORT}", trust_env=False, timeout=10
        ) as node:
            headers = {"Authorization": "Bearer " + settings.node_token.get_secret_value()}
            loaded = await node.post(
                "/node/engines",
                headers=headers,
                json={
                    "engine_id": str(engine_id),
                    "slug": source.name,
                    "estimate_bytes": 1024**3,
                    "target": target.model_dump(mode="json"),
                },
            )
            assert loaded.status_code == 202, loaded.status_code
            deadline = time.monotonic() + 120
            while True:
                engine = agent.engines.get(engine_id)
                assert engine is not None and engine.state is not EngineState.FAILED
                if engine.state is EngineState.READY:
                    break
                assert time.monotonic() < deadline
                await asyncio.sleep(0.1)
            placement = single_placement(uuid4(), names[1], engine)
            assert placement is not None
            now = datetime.now(UTC)
            await asyncio.to_thread(
                Path(settings.failover_safety_path).write_text,
                ElectionSafetyRecord(
                    member=names[1], epoch=1, highest_term=1, prior_max_lifetime_s=15
                ).model_dump_json(),
            )
            participant = StudioParticipant(
                name=names[1],
                membership=membership,
                private_key_b64=keys[names[1]],
                proof_path=Path(settings.failover_proof_path),
                safety_path=Path(settings.failover_safety_path),
                timings=timings_from_settings(settings),
            )
            node_routes.set_participant(participant)
            issuer = f"https://{HOST}:{IDENTITY_PORT}"
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
                        issuer=issuer, subject="operator", eligible=True, policy_version=1
                    )
                ],
                access_verifier=FailoverAccessVerifier(
                    issuer=issuer, audience="native-container", jwks_url=issuer + "/keys"
                ),
                signature="unsigned",
            )
            snapshot = snapshot.model_copy(
                update={"signature": sign_ed25519(snapshot.canonical_bytes(), keys[names[0]])}
            )
            await asyncio.to_thread(
                Path(settings.failover_snapshot_path).write_text, snapshot.model_dump_json()
            )
            observations = [
                MemberObservation(
                    name=name,
                    health=MemberHealth.UNREACHABLE if name == names[0] else MemberHealth.HEALTHY,
                    since=now - timedelta(seconds=20),
                )
                for name in names
            ]

            async def quorum() -> None:
                while True:
                    moment = datetime.now(UTC)
                    participant.observe(observations, moment)
                    grant = ElectionVoteGrant(
                        epoch=1,
                        term=participant.term,
                        candidate=names[1],
                        voter=names[2],
                        issued_at=moment,
                        expires_at=moment + timedelta(seconds=15),
                        signature="unsigned",
                    )
                    participant.receive_grant(
                        grant.model_copy(
                            update={
                                "signature": sign_ed25519(grant.canonical_bytes(), keys[names[2]])
                            }
                        ),
                        moment,
                    )
                    participant.observe(observations, moment)
                    await asyncio.sleep(2)

            renew = asyncio.create_task(quorum())
            await asyncio.sleep(0)
            assert participant.serving()
            controller = StudioFailoverController(settings, docker=frontend.docker)
            controller._poller = FailoverPoller(participant, settings)
            swap_before = psutil.swap_memory().used
            await controller._sync_frontend()
            deadline = time.monotonic() + 15
            while not (await frontend.observe())["ready"]:
                assert time.monotonic() < deadline, "frontend did not become ready"
                await asyncio.sleep(0.2)
            token = jwt.encode(
                {
                    "iss": issuer,
                    "aud": "native-container",
                    "sub": "operator",
                    "email": "operator@qualification.test",
                    "iat": int(now.timestamp()),
                    "exp": int(now.timestamp()) + 90,
                },
                signing,
                algorithm="RS256",
                headers={"kid": "qualification"},
            )
            # The exec observer is inside the actual container; requests use its real HTTP
            # listener and configured native node relay. No ASGI or HTTP mock transport.
            request = {"token": token, "model": str(model.id)}
            script = """import json,sys,urllib.request,urllib.error
v=json.load(sys.stdin)
results=[]
for stream in (False,True):
 body=json.dumps({'model':v['model'],'messages':[{'role':'user','content':'Say hello.'}],
                  'max_tokens':8,'stream':stream}).encode()
 req=urllib.request.Request('http://127.0.0.1:8004/v1/chat/completions',data=body,
       headers={'Content-Type':'application/json','Cf-Access-Jwt-Assertion':v['token']})
 try:
  response=urllib.request.urlopen(req,timeout=20); data=response.read().decode()
 except urllib.error.HTTPError as error:
  print(json.dumps({'status':error.code,'body':error.read().decode()}));raise
 assert '[DONE]' in data if stream else json.loads(data)['choices'][0]['message']['content']
 results.append({'status':response.status,'stream':stream,'completed':True})
print(json.dumps(results))
"""
            process = await asyncio.create_subprocess_exec(
                "/usr/local/bin/docker",
                "exec",
                "-i",
                container,
                "/app/.venv/bin/python3",
                "-c",
                script,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _err = await asyncio.wait_for(
                process.communicate(json.dumps(request).encode()), 30
            )
            assert process.returncode == 0, out.decode()[:1000]
            completed = json.loads(out)
            actual = agent.engines.get(engine_id)
            assert actual and actual.state is EngineState.READY
            assert (
                actual.pid == engine.pid
                and actual.process_create_time == engine.process_create_time
            )
            assert len(agent.engines.statuses()) == 1
            assert psutil.swap_memory().used == swap_before
            stats = await frontend.docker.stats(container)
            assert stats and stats["memory_stats"]["usage"] <= 256 * 1024**2
            result = {
                "source": args.source,
                "runtime_source_identity": identity_hashes,
                "fixture_sha256": fixture_hash,
                "image": args.image,
                "model_slug": source.name,
                "model_manifest_sha256": manifest.sha256(),
                "engine_id": str(engine_id),
                "engine_pid": engine.pid,
                "engine_process_create_time": engine.process_create_time,
                "scope": "real container HTTP to native node/engine; fixture TLS identity and quorum",
                "cloudflare": False,
                "physical_core_loss": False,
                "requests": completed,
                "frontend_rss_mib": stats["memory_stats"]["usage"] / 1024**2,
                "swap_growth_bytes": 0,
                "engine_identity_preserved": True,
                "root": str(root),
                "anonymous_refused": (await frontend.observe())["anonymous_refused"],
                "cleanup_completed": False,
            }
            await asyncio.to_thread(
                Path(args.result).write_text, json.dumps(result, indent=2) + "\n"
            )
    finally:
        cleanup_errors: list[Exception] = []

        async def cleanup(operation: Awaitable[object]) -> None:
            try:
                await operation
            except Exception as error:
                cleanup_errors.append(error)

        if renew:
            renew.cancel()
            await asyncio.gather(renew, return_exceptions=True)
        if controller:
            await cleanup(controller.stop())
        await cleanup(frontend.close())
        node_routes.set_participant(None)
        node_routes._client = None
        if access:
            await cleanup(access.aclose())

        async def stop_engine() -> None:
            await asyncio.to_thread(agent.engines.stop, engine_id)
            deadline = time.monotonic() + 15
            while (remaining := agent.engines.get(engine_id)) is not None:
                if remaining.state is EngineState.STOPPED:
                    return
                assert time.monotonic() < deadline, "owned engine cleanup did not complete"
                await asyncio.sleep(0.1)

        await cleanup(stop_engine())
        for server in servers:
            server.should_exit = True
        await cleanup(asyncio.wait_for(asyncio.gather(*tasks), timeout=10))
        await cleanup(asyncio.to_thread(agent.close))
        if cleanup_errors:
            raise ExceptionGroup("Native frontend fixture cleanup failed", cleanup_errors)
        await asyncio.to_thread(shutil.rmtree, root)
        await asyncio.to_thread(shutil.rmtree, root.with_name(root.name + "-secrets"))
    assert result is not None
    result["cleanup_completed"] = True
    await asyncio.to_thread(Path(args.result).write_text, json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-temporary-control-listeners", action="store_true")
    parser.add_argument("--model", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--expected-source-hashes", required=True)
    parser.add_argument("--result", required=True)
    asyncio.run(main(parser.parse_args()))
