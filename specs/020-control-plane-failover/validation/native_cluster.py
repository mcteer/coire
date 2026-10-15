"""Operator-run isolated two-Studio acceptance. Never invoked by CI.

Copy the current checkout to /tmp/coire-020-qualification on both Studios, uv sync --frozen,
then run this module on Core. SSH carries the control requests through Unix sockets/stdin;
no listener or firewall policy is broadened. Only native node managers spawn MLX workers.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = "/tmp/coire-020-qualification"
WORK = "/tmp/coire-020-sharded-" + str(uuid4())
MODE = os.environ.get("COIRE_QUALIFY_SHARD_MODE", "tp")
FAULT_RANK = int(os.environ.get("COIRE_QUALIFY_FAULT_RANK", "1"))
MODEL = os.environ.get(
    "COIRE_QUALIFY_MODEL", "/opt/coire/models/mlx-community--Qwen2.5-0.5B-Instruct-4bit"
)
assert MODE in {"tp", "pp"}
assert FAULT_RANK in (0, 1)
SCRIPT = "specs/020-control-plane-failover/validation/native_cluster.py"
NAMES = ("coire-core", "coire-edge-a", "coire-edge-b")


def acquired_asset(value: str) -> Path:
    """Verify an operator-selected acquired asset; CI's tiny-model gate is separate."""
    from coire_node.store import Store

    assert os.uname().nodename.split(".")[0] in NAMES[1:], "native work requires a Studio"
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("qualification requires an acquired local directory")
    path = path.resolve()
    if not path.is_relative_to("/opt/coire/models"):
        raise ValueError("qualification asset must belong to the acquired model store")
    store = Store(path.parent)
    manifest_path = store.manifest_path(path.name)
    manifest = store.read_manifest(path.name)
    if manifest_path.is_symlink() or manifest is None or manifest.slug != path.name:
        raise ValueError("qualification requires the acquisition checksum manifest")
    for file in path.rglob("*"):
        if file.relative_to(path).parts[0] == ".cache":
            continue
        if file.is_symlink() or (file.is_file() and file.stat().st_nlink != 1):
            raise ValueError("qualification asset contains linked model files")
    if not manifest.files or len(manifest.by_path()) != len(manifest.files):
        raise ValueError("qualification manifest must contain unique files")
    if (
        store.verify_against(path.name, manifest)
        or store.size_bytes(path.name) != manifest.total_bytes
    ):
        raise ValueError("qualification asset failed acquisition checksum verification")
    config = json.loads((path / "config.json").read_bytes())
    if not isinstance(config, dict) or config.get("auto_map") or config.get("model_file"):
        raise ValueError("qualification asset requires executable model configuration")
    if not any(entry.path.endswith(".safetensors") for entry in manifest.files):
        raise ValueError("qualification asset has no safetensors weights")
    return path


async def worker() -> None:
    import httpx
    import uvicorn
    from pydantic import SecretStr

    from coire_core.failover_election import ElectionTimings, MemberHealth, MemberObservation
    from coire_core.models import ShardingMode
    from coire_core.models.failover import (
        ElectionSafetyRecord,
        ElectionVoteGrant,
        FailoverMembershipConfig,
        FailoverSnapshot,
    )
    from coire_node.failover.participant import StudioParticipant
    from coire_node.metrics import MetricsCollector
    from coire_node.routes import failover
    from coire_node.testing.harness import TOKEN, Agent

    initial = json.loads(sys.stdin.readline())
    name = initial["name"]
    assert os.uname().nodename.split(".")[0] == name
    source = await asyncio.to_thread(acquired_asset, initial["model"])
    root = Path(initial["work"])
    await asyncio.to_thread(root.mkdir, mode=0o700, exist_ok=False)
    agent = Agent(
        root,
        node_name=name,
        sharding_jaccl_hostfile="/opt/coire/state/jaccl-hostfile.json",
        sharding_ring_hostfile="/opt/coire/state/ring-hostfile.json",
        sharding_start_timeout_s=120,
    )
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
    settings = agent.settings
    settings.failover_member_name = name
    settings.failover_peer_key = SecretStr(initial["private"])
    settings.failover_core_public_key = initial["core_public"]
    settings.failover_snapshot_path = str(root / "snapshot.json")
    settings.failover_relay_token = SecretStr(initial["relay_token"])
    membership = FailoverMembershipConfig.model_validate(initial["membership"])
    safety = root / "safety.json"
    safety.write_text(
        ElectionSafetyRecord(
            member=name, epoch=1, highest_term=1, prior_max_lifetime_s=15
        ).model_dump_json()
    )
    participant = StudioParticipant(
        name=name,
        membership=membership,
        private_key_b64=initial["private"],
        proof_path=root / "proof.json",
        safety_path=safety,
        timings=ElectionTimings(
            promotion=timedelta(seconds=15),
            demotion=timedelta(seconds=45),
            lease=timedelta(seconds=15),
            drain=timedelta(seconds=30),
        ),
    )
    failover.set_participant(participant)
    collector = MetricsCollector(
        node_name=name,
        agent_version="020-qualification",
        interval_s=2,
        budget_cpu_pct=1,
        budget_rss_bytes=32 * 1024**2,
    )
    app = agent.app()
    app.state.failover_measure_link = collector.data_link_status
    uds = root / "node.sock"
    started = asyncio.Event()

    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            started.set()

    server = ReadyServer(uvicorn.Config(app, uds=str(uds), log_level="warning"))
    serving = asyncio.create_task(server.serve())
    await asyncio.wait_for(started.wait(), timeout=10)
    mounts = {
        "https://isolated.fixture": httpx.MockTransport(
            lambda _: httpx.Response(200, json={"keys": [initial["jwk"]]})
        )
    }
    forward = None
    if name == "coire-edge-a":
        peer_uds = root / "peer.sock"
        known_hosts = root / "known_hosts"
        await asyncio.to_thread(
            known_hosts.write_text, "coire-edge-b.lab " + initial["peer_host_key"] + "\n"
        )
        forward = await asyncio.create_subprocess_exec(
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "ExitOnForwardFailure=yes",
            "-N",
            "-L",
            f"{peer_uds}:{root}/node.sock",
            "coire-edge-b.lab",
            stdout=subprocess.DEVNULL,
        )
        mounts["http://coire-edge-b.lab:9400"] = httpx.AsyncHTTPTransport(uds=str(peer_uds))
    upstream = httpx.AsyncClient(mounts=mounts)
    failover._client = upstream

    def output(value: object) -> None:
        print(json.dumps(value), flush=True)

    output(
        {
            "manifest": manifest.sha256(),
            "slug": source.name,
            "bytes": manifest.total_bytes,
            "supported": (
                await asyncio.to_thread(
                    agent.shard_groups.capability, source.name, ShardingMode(initial["mode"])
                )
            ).supported,
            "hostfile": hashlib.sha256(
                await asyncio.to_thread(
                    Path(
                        settings.sharding_jaccl_hostfile
                        if initial["mode"] == "tp"
                        else settings.sharding_ring_hostfile
                    ).read_bytes
                )
            ).hexdigest(),
            "swap": (
                await asyncio.to_thread(
                    subprocess.check_output, ["/usr/sbin/sysctl", "-n", "vm.swapusage"], text=True
                )
            ).strip(),
        }
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://node"
        ) as client:
            while line := await asyncio.to_thread(sys.stdin.readline):
                command = json.loads(line)
                operation = command["operation"]
                if operation == "exit":
                    break
                if operation == "arm":
                    snapshot = FailoverSnapshot.model_validate(command["snapshot"])
                    await asyncio.to_thread(
                        Path(settings.failover_snapshot_path).write_text, snapshot.model_dump_json()
                    )
                    now = datetime.now(UTC)
                    view = [
                        MemberObservation(
                            name=peer,
                            health=MemberHealth.UNREACHABLE
                            if peer == "coire-core"
                            else MemberHealth.HEALTHY,
                            since=now - timedelta(seconds=20),
                        )
                        for peer in NAMES
                    ]
                    participant.observe(view, now)
                    if name == "coire-edge-a":
                        grant = ElectionVoteGrant.model_validate(command["grant"])
                        assert participant.receive_grant(grant, now)
                        participant.observe(view, now)
                    output(
                        {
                            "role": participant.role,
                            "link": collector.data_link_status().model_dump(mode="json"),
                        }
                    )
                elif operation == "kill_rank":
                    from uuid import UUID

                    import psutil

                    rank = agent.shard_groups.local_rank(UUID(command["group_id"]))
                    assert rank and rank.pid and rank.process_create_time
                    process = psutil.Process(rank.pid)
                    assert process.create_time() == rank.process_create_time
                    process.terminate()  # exact owned qualification worker, never an arbitrary PID
                    output({"terminated_rank": rank.rank})
                elif operation == "swap":
                    output(
                        {
                            "swap": (
                                await asyncio.to_thread(
                                    subprocess.check_output,
                                    ["/usr/sbin/sysctl", "-n", "vm.swapusage"],
                                    text=True,
                                )
                            ).strip()
                        }
                    )
                else:
                    response = await client.request(
                        command["method"],
                        command["path"],
                        json=command.get("body"),
                        headers=command.get("headers", {"Authorization": f"Bearer {TOKEN}"}),
                        timeout=150,
                    )
                    if response.status_code >= 400:
                        print(repr(agent.shard_groups._outputs), file=sys.stderr)
                    output({"status": response.status_code, "body": response.text})
    finally:
        for group_id in list(agent.shard_groups._groups):
            agent.shard_groups.stop(group_id)
        failover.set_participant(None)
        server.should_exit = True
        await serving
        await upstream.aclose()
        if forward:
            forward.terminate()
            await asyncio.wait_for(forward.wait(), timeout=5)
        agent.close()
        # Only independent copies in this run's owned model store are removed.
        # Native groups have been stopped above; acquired source bytes stay intact.
        await asyncio.to_thread(shutil.rmtree, agent.store.root)


def run() -> None:
    import secrets
    import select

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from coire_core.failover_crypto import public_key_b64, sign_ed25519
    from coire_core.failover_residency import sharded_placement
    from coire_core.models.failover import (
        ElectionVoteGrant,
        FailoverAccessSubject,
        FailoverAccessVerifier,
        FailoverMember,
        FailoverMembershipConfig,
        FailoverModel,
        FailoverSnapshot,
    )
    from coire_core.models.sharding import ShardGroupStatus

    private = {
        name: base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()
        for name in NAMES
    }
    membership = FailoverMembershipConfig(
        epoch=1,
        signing_key_id="isolated-cluster",
        members=[
            FailoverMember(name=name, priority=i, public_key=public_key_b64(private[name]))
            for i, name in enumerate(NAMES)
        ],
    )
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing.public_key(), as_dict=True)
    jwk.update(kid="test", alg="RS256", use="sig")
    relay_token = secrets.token_urlsafe(24)
    workers = {}
    peer_host_key = subprocess.check_output(
        ["ssh", "-o", "BatchMode=yes", "coire-edge-b.lab", "cat /etc/ssh/ssh_host_ed25519_key.pub"],
        text=True,
    ).strip()

    def receive(name: str) -> dict:
        stream = workers[name].stdout
        assert select.select([stream], [], [], 160)[0], f"{name} timed out"
        line = stream.readline()
        assert line, f"{name} worker exited; inspect its private qualification log"
        return json.loads(line)

    def rpc(name: str, **value: object) -> dict:
        workers[name].stdin.write(json.dumps(value) + "\n")
        workers[name].stdin.flush()
        return receive(name)

    def request(
        name: str, method: str, path: str, body: object = None, headers: dict | None = None
    ) -> dict:
        value = {"operation": "request", "method": method, "path": path, "body": body}
        if headers:
            value["headers"] = headers
        result = rpc(name, **value)
        assert result["status"] in (200, 202), result
        return json.loads(result["body"])

    results = {}
    group_id = str(uuid4())
    try:
        for name in reversed(NAMES[1:]):
            with open(f"/tmp/coire-020-{name}-sharded.log", "w") as log:
                process = subprocess.Popen(
                    [
                        "ssh",
                        "-o",
                        "BatchMode=yes",
                        name + ".lab",
                        f"cd {ROOT} && .venv/bin/python {SCRIPT} --worker",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    text=True,
                    bufsize=1,
                )
            workers[name] = process
            process.stdin.write(
                json.dumps(
                    {
                        "name": name,
                        "work": WORK,
                        "mode": MODE,
                        "model": MODEL,
                        "private": private[name],
                        "core_public": public_key_b64(private[NAMES[0]]),
                        "membership": membership.model_dump(mode="json"),
                        "jwk": jwk,
                        "relay_token": relay_token,
                        "peer_host_key": peer_host_key,
                    }
                )
                + "\n"
            )
            process.stdin.flush()
            results[name] = receive(name)
        assert all(results[name]["supported"] for name in NAMES[1:]), (
            "acquired model does not support this sharding mode"
        )
        assert results[NAMES[1]]["manifest"] == results[NAMES[2]]["manifest"], (
            "acquired replica artifacts differ"
        )
        assert results[NAMES[1]]["hostfile"] == results[NAMES[2]]["hostfile"]
        model_id, variant_id = str(uuid4()), str(uuid4())
        command = {
            "command_id": str(uuid4()),
            "group_id": group_id,
            "instance_id": str(uuid4()),
            "variant_id": variant_id,
            "target": {
                "model_id": model_id,
                "variant_id": variant_id,
                "base_manifest_sha256": results[NAMES[1]]["manifest"],
            },
            "slug": results[NAMES[1]]["slug"],
            "mode": MODE,
            "estimate_bytes_per_rank": max(1024**3, results[NAMES[1]]["bytes"] * 2),
            "hostfile_sha256": results[NAMES[1]]["hostfile"],
            "ranks": [
                {"rank": i, "node_name": name, "host": name + ".fabric", "port": 9600}
                for i, name in enumerate(NAMES[1:])
            ],
        }
        for name in reversed(NAMES[1:]):
            request(name, "POST", "/node/shard-groups", command)
        for name in reversed(NAMES[1:]):
            request(name, "POST", f"/node/shard-groups/{group_id}/ready")
        statuses = {
            name: ShardGroupStatus.model_validate(
                request(name, "GET", f"/node/shard-groups/{group_id}")
            )
            for name in NAMES[1:]
        }
        placement = sharded_placement(statuses)
        assert placement is not None, "native worker ownership was not established"
        now = datetime.now(UTC)
        model = FailoverModel(
            id=model_id,
            slug="native-sharded",
            display_name="Native sharded",
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
                audience="cluster-test",
                jwks_url="https://isolated.fixture/keys",
            ),
            signature="unsigned",
        )
        snapshot = snapshot.model_copy(
            update={"signature": sign_ed25519(snapshot.canonical_bytes(), private[NAMES[0]])}
        )
        grant = ElectionVoteGrant(
            epoch=1,
            term=1,
            candidate=NAMES[1],
            voter=NAMES[2],
            issued_at=now,
            expires_at=now + timedelta(seconds=15),
            signature="unsigned",
        )
        grant = grant.model_copy(
            update={"signature": sign_ed25519(grant.canonical_bytes(), private[NAMES[2]])}
        )
        for name in reversed(NAMES[1:]):
            results[name]["armed"] = rpc(
                name,
                operation="arm",
                snapshot=snapshot.model_dump(mode="json"),
                grant=grant.model_dump(mode="json"),
            )
        assertion = jwt.encode(
            {
                "iss": "https://isolated.fixture",
                "aud": "cluster-test",
                "sub": "operator",
                "email": "operator@example.test",
                "iat": int(now.timestamp()),
                "exp": int(now.timestamp()) + 60,
            },
            signing,
            algorithm="RS256",
            headers={"kid": "test"},
        )
        headers = {"X-Coire-Failover-Relay": relay_token}
        resident = request(NAMES[1], "GET", "/node/failover/resident", headers=headers)
        assert len(resident) == 1, resident
        for stream in (False, True):
            body = {
                "model": model_id,
                "messages": [{"role": "user", "content": "Say hello."}],
                "max_tokens": 8,
                "stream": stream,
                "coire_wait_for_model": False,
            }
            authority = request(
                NAMES[1],
                "POST",
                "/node/failover/authority",
                {"engine_id": group_id, "request": body, "access_assertion": assertion},
                headers,
            )
            result = rpc(
                NAMES[1],
                operation="request",
                method="POST",
                path=f"/node/failover/engines/{group_id}/chat/completions",
                headers=headers,
                body={
                    "engine_id": group_id,
                    "model_slug": model.slug,
                    "request": body,
                    "authority": authority,
                    "access_assertion": assertion,
                },
            )
            assert result["status"] == 200, result
            if stream:
                assert "[DONE]" in result["body"]
            else:
                completion = json.loads(result["body"])
                message = completion["choices"][0]["message"]
                # Reasoning models may spend the bounded eight-token fixture entirely
                # in the reasoning channel. Require actual generated text and usage.
                assert any(
                    isinstance(message.get(channel), str) and message[channel].strip()
                    for channel in ("content", "reasoning_content", "reasoning")
                ), message
                assert completion["usage"]["completion_tokens"] > 0
                results["buffered_completion_tokens"] = completion["usage"]["completion_tokens"]
        started = time.monotonic()
        rpc(NAMES[FAULT_RANK + 1], operation="kill_rank", group_id=group_id)
        while request(NAMES[1], "GET", "/node/failover/resident", headers=headers):
            assert time.monotonic() - started < 3, "failed rank remained admissible"
            time.sleep(0.05)
        results["failed_rank"] = FAULT_RANK
        results["rank_loss_refusal_s"] = time.monotonic() - started
        assert results["rank_loss_refusal_s"] < 3
        results["placement"] = placement.model_dump(mode="json")
        results["native_buffered_and_streaming"] = "passed"
        for name in NAMES[1:]:
            results[name]["after"] = rpc(name, operation="swap")
        print(json.dumps(results, indent=2))
    finally:
        for process in workers.values():
            if process.poll() is None:
                try:
                    process.stdin.write('{"operation":"exit"}\n')
                    process.stdin.flush()
                    process.wait(timeout=45)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    process.terminate()
                    process.wait(timeout=5)


if __name__ == "__main__":
    asyncio.run(worker()) if "--worker" in sys.argv else run()
