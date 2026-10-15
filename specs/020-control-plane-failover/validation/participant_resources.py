"""Operator-only participant measurements on Core and both Studios.

Uses the production signed HTTP routes, subscriber, poller and durable safety files.
SSH forwards private Unix sockets; it creates no public listener or network rule.
The isolated Core publishes empty signed snapshots. No engines or application data
are loaded. The normal Studio metrics collector runs in both windows. An optional
preloaded frontend image includes the Docker lifecycle loop; inference coexistence
requires separate acceptance.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import statistics
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

NAMES = ("coire-core", "coire-edge-a", "coire-edge-b")
REMOTE = "/tmp/coire-020-qualification"
SCRIPT = "specs/020-control-plane-failover/validation/participant_resources.py"
SOURCES = (
    "packages/coire-core/src/coire_core/models/failover.py",
    "packages/coire-core/src/coire_core/failover_election.py",
    "packages/coire-core/src/coire_core/failover_crypto.py",
    "packages/coire-core/src/coire_core/failover_poller.py",
    "packages/coire-core/src/coire_core/failover_journal.py",
    "packages/coire-core/src/coire_core/failover_safety.py",
    "packages/coire-core/src/coire_core/failover_telemetry.py",
    "apps/coire-node/src/coire_node/metrics.py",
    "apps/coire-node/src/coire_node/native_probe.py",
    "apps/coire-node/src/coire_node/otel.py",
    "apps/coire-node/src/coire_node/failover/participant.py",
    "apps/coire-node/src/coire_node/failover/poller.py",
    "apps/coire-node/src/coire_node/routes/failover.py",
    "apps/coire-api/src/coire_api/routes/failover.py",
    "apps/coire-api/src/coire_api/failover/participant.py",
    SCRIPT,
    "specs/020-control-plane-failover/validation/frontend_container.py",
)


def source_identity() -> dict[str, str]:
    return {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in SOURCES}


async def worker() -> None:
    import httpx
    import psutil
    import uvicorn
    from fastapi import FastAPI
    from pydantic import SecretStr

    from coire_core.failover_crypto import sign_ed25519
    from coire_core.failover_poller import FailoverPoller, HttpPeerExchange
    from coire_core.models.failover import (
        FailoverAccessVerifier,
        FailoverMembershipConfig,
        FailoverSnapshot,
    )
    from coire_core.settings import Settings
    from coire_node.otel import configure_node_telemetry

    initial = json.loads(sys.stdin.readline())
    assert await asyncio.to_thread(source_identity) == initial["source_identity"], (
        "qualification checkout differs from the selected source"
    )
    name = initial["name"]
    assert os.uname().nodename.split(".")[0] == name
    root = Path(initial["root"])
    await asyncio.to_thread(
        root.mkdir, mode=0o700, parents=True, exist_ok=initial.get("resume", False)
    )
    details = await asyncio.to_thread(root.lstat)
    assert not await asyncio.to_thread(root.is_symlink) and details.st_uid == os.getuid()
    assert details.st_mode & 0o077 == 0
    configure_node_telemetry(
        "020-resource-acceptance",
        "http://127.0.0.1:1",
        diagnostics_enabled=initial["diagnostics"],
    )
    membership = FailoverMembershipConfig.model_validate(initial["membership"])
    settings = Settings(
        _secrets_dir="/nonexistent",
        failover_member_name=name,
        failover_peer_key=SecretStr(initial["key"]),
        failover_core_public_key=membership.members[0].public_key,
        failover_snapshot_path=str(root / "snapshot.json"),
        failover_proof_path=str(root / "proof.json"),
        failover_safety_path=str(root / "safety.json"),
    )
    mounts = {
        f"http://{peer}.lab:{settings.core_api_port if peer == NAMES[0] else settings.node_listen_port}": httpx.AsyncHTTPTransport(
            uds=str(root / f"{peer}.sock")
        )
        for peer in NAMES
        if peer != name
    }

    async def response_diagnostic(response: httpx.Response) -> None:
        if response.request.url.path.endswith("/snapshot"):
            await response.aread()
            print(
                json.dumps(
                    {
                        "snapshot_http_status": response.status_code,
                        "host": name,
                        "url": str(response.request.url),
                    }
                ),
                file=sys.stderr,
            )
            if response.status_code == 200:
                value = FailoverSnapshot.model_validate(response.json())
                print(
                    json.dumps(
                        {
                            "signature_valid": value.signature_is_valid(
                                settings.failover_core_public_key
                            ),
                            "fresh": value.is_fresh(120),
                            "age_s": (datetime.now(UTC) - value.issued_at).total_seconds(),
                        }
                    ),
                    file=sys.stderr,
                )

    client = httpx.AsyncClient(
        mounts=mounts, trust_env=False, event_hooks={"response": [response_diagnostic]}
    )
    agent = None
    collector = None
    controller = None
    poller = None
    publish = None
    frontend = None
    snapshot_ready = asyncio.Event()
    if name == NAMES[0]:
        from coire_api.failover.participant import core_participant
        from coire_api.routes import failover

        participant = core_participant(settings, membership)
        assert participant is not None
        # This resource fixture has no application cohort; readiness is a fixture input.
        participant.recovery_ready = True
        failover.set_participant(participant)
        failover.get_settings = lambda: settings
        app = FastAPI()
        app.include_router(failover.router)
        poller = FailoverPoller(
            participant, settings, exchange=HttpPeerExchange(participant, settings, client)
        )

        def signed_snapshot() -> FailoverSnapshot:
            now = datetime.now(UTC)
            value = FailoverSnapshot(
                snapshot_id=uuid4(),
                issued_at=now,
                expires_at=now + timedelta(seconds=120),
                membership=membership,
                access_verifier=FailoverAccessVerifier(
                    issuer="https://isolated.fixture",
                    audience="resource-test",
                    jwks_url="https://isolated.fixture/keys",
                ),
                signature="unsigned",
            )
            return value.model_copy(
                update={"signature": sign_ed25519(value.canonical_bytes(), initial["key"])}
            )

        signed_snapshot()  # Validate the complete wire fixture before the baseline window.

        async def snapshots() -> None:
            while True:
                value = signed_snapshot()
                temporary = root / "snapshot.tmp"
                await asyncio.to_thread(temporary.write_text, value.model_dump_json())
                await asyncio.to_thread(temporary.replace, root / "snapshot.json")
                snapshot_ready.set()
                await asyncio.sleep(30)
    else:
        from coire_node.failover.poller import StudioFailoverController
        from coire_node.metrics import MetricsCollector
        from coire_node.testing.harness import Agent

        agent = Agent(root / "agent", node_name=name)
        # The normal node collects metrics even with failover disabled. Preserve
        # that baseline, including its OTLP exporter, in both measurement windows.
        collector = MetricsCollector(
            node_name=name,
            agent_version="020-resource-acceptance",
            interval_s=agent.settings.node_probe_interval_s,
            budget_cpu_pct=agent.settings.node_collection_budget_cpu_pct,
            budget_rss_bytes=agent.settings.node_collection_budget_rss_bytes,
        )
        collector.attach(
            store=agent.store,
            jobs=agent.jobs,
            engines=agent.engines,
            reservations=agent.reservations,
        )
        agent.collector = collector
        await asyncio.to_thread(collector.start)
        if initial.get("frontend_image"):
            from frontend_container import FrontendContainer

            frontend = FrontendContainer(root, settings, initial["frontend_image"])
            try:
                await frontend.prepare()
            except BaseException:
                await frontend.close()
                raise
        for field in type(settings).model_fields:
            if field.startswith("failover_"):
                setattr(agent.settings, field, getattr(settings, field))
        app = agent.app()
        controller = StudioFailoverController(
            agent.settings, client=client, docker=frontend.docker if frontend else None
        )
    started = asyncio.Event()

    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            started.set()

    server = ReadyServer(
        uvicorn.Config(app, uds=str(root / "node.sock"), lifespan="off", log_level="critical")
    )
    serving = asyncio.create_task(server.serve())
    await asyncio.wait_for(started.wait(), timeout=10)
    process = psutil.Process()

    def sample() -> dict:
        cpu = process.cpu_times()
        # Read the kernel counter directly. Forking sysctl inside an instrumented
        # process invokes gRPC's fork handlers and can distort CPU or hang the sampler.
        swap_used_mib = psutil.swap_memory().used / 1024**2
        member = (
            poller.participant
            if poller
            else (controller._poller.participant if controller and controller._poller else None)
        )
        return {
            "at": time.monotonic(),
            "rss": process.memory_info().rss,
            "cpu_s": cpu.user + cpu.system,
            "swap_used_mib": swap_used_mib,
            "role": member.role.value if member else "unarmed",
            "pid": process.pid,
        }

    def output(value: object) -> None:
        print(json.dumps(value), flush=True)

    output({"ready": True, "sample": sample()})
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            if command["op"] == "exit":
                break
            if publish is not None and publish.done():
                publish.result()  # Background publication failure is a failed run.
            if command["op"] == "arm":
                if poller:
                    publish = asyncio.create_task(snapshots())
                    await asyncio.wait_for(snapshot_ready.wait(), timeout=5)
                    await poller.start()
                else:
                    assert controller is not None
                    deadline = time.monotonic() + 5
                    while not await controller.refresh_once():
                        assert time.monotonic() < deadline, "signed snapshot preflight failed"
                        await asyncio.sleep(0.25)
                    await controller.start()
            measured = await asyncio.to_thread(sample)
            if frontend:
                measured["frontend"] = await frontend.observe()
            output(measured)
    finally:
        if publish:
            publish.cancel()
            await asyncio.gather(publish, return_exceptions=True)
        if poller:
            await poller.stop()
        if controller:
            await controller.stop()
        if frontend:
            await frontend.close()
        server.should_exit = True
        await serving
        await client.aclose()
        if collector:
            await asyncio.to_thread(collector.stop)
        if agent:
            agent.close()


async def run() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from coire_core.failover_crypto import public_key_b64
    from coire_core.models.failover import FailoverMember, FailoverMembershipConfig

    assert os.uname().nodename.split(".")[0] == NAMES[0]
    diagnostics = "--diagnostics" in sys.argv
    duration = int(os.environ.get("COIRE_RESOURCE_WINDOW_S", "900"))
    assert duration >= 900, "acceptance requires full fifteen-minute windows"
    root = Path("/tmp/coire-020-resources-" + uuid4().hex[:10])
    output = Path(os.environ["COIRE_RESOURCE_RESULT"])
    keys = {
        name: base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()
        for name in NAMES
    }
    membership = FailoverMembershipConfig(
        epoch=1,
        signing_key_id="isolated-resource-test",
        members=[
            FailoverMember(name=name, priority=i, public_key=public_key_b64(keys[name]))
            for i, name in enumerate(NAMES)
        ],
    )
    workers = {}
    initials = {}
    forwards = []
    logs = []
    log_stack = ExitStack()
    measurements = {name: {} for name in NAMES}
    selected_source = await asyncio.to_thread(source_identity)

    def open_log(name: str):
        return log_stack.enter_context(Path(str(output) + "." + name + ".log").open("w"))

    async def receive(name: str) -> dict:
        line = await asyncio.wait_for(workers[name].stdout.readline(), timeout=30)
        assert line, f"{name} resource worker exited; inspect its private log"
        return json.loads(line)

    async def rpc(name: str, operation: str) -> dict:
        workers[name].stdin.write(json.dumps({"op": operation}).encode() + b"\n")
        await workers[name].stdin.drain()
        return await receive(name)

    try:
        for name in NAMES:
            log = await asyncio.to_thread(open_log, name)
            logs.append(log)
            command = (
                [sys.executable, SCRIPT, "--worker"]
                if name == NAMES[0]
                else [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    name + ".lab",
                    f"cd {REMOTE} && .venv/bin/python {SCRIPT} --worker",
                ]
            )
            workers[name] = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=log
            )
            initials[name] = {
                "name": name,
                "root": str(root),
                "key": keys[name],
                "membership": membership.model_dump(mode="json"),
                "diagnostics": diagnostics,
                "source_identity": selected_source,
                "frontend_image": os.environ.get("COIRE_RESOURCE_FRONTEND_IMAGE", ""),
            }
            workers[name].stdin.write(json.dumps(initials[name]).encode() + b"\n")
            await workers[name].stdin.drain()
            assert (await receive(name))["ready"]
        for name in NAMES[1:]:
            other = NAMES[2] if name == NAMES[1] else NAMES[1]
            forwards.append(
                await asyncio.create_subprocess_exec(
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-N",
                    "-L",
                    f"{root}/{name}.sock:{root}/node.sock",
                    "-R",
                    f"{root}/{NAMES[0]}.sock:{root}/node.sock",
                    "-R",
                    f"{root}/{other}.sock:{root}/{other}.sock",
                    name + ".lab",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=logs[0],
                )
            )
        await asyncio.sleep(2)
        assert all(process.returncode is None for process in forwards)
        if "--preflight" in sys.argv:
            await rpc(NAMES[0], "arm")
            samples = await asyncio.gather(*(rpc(name, "arm") for name in NAMES[1:]))
            assert all(sample["role"] != "unarmed" for sample in samples)
            print("signed snapshot/subscriber preflight passed on both Studios", flush=True)
            return
        if "--faults" in sys.argv:
            await rpc(NAMES[0], "arm")
            await asyncio.gather(*(rpc(name, "arm") for name in NAMES[1:]))
            transitions = []

            async def wait_roles(expected: dict[str, str], bound: float) -> float:
                started_at = time.monotonic()
                while True:
                    samples = await asyncio.gather(*(rpc(name, "sample") for name in expected))
                    roles = {
                        name: item["role"] for name, item in zip(expected, samples, strict=True)
                    }
                    assert sum(role == "elected" for role in roles.values()) <= 1, roles
                    elapsed = time.monotonic() - started_at
                    frontends = {
                        name: item["frontend"]
                        for name, item in zip(expected, samples, strict=True)
                        if "frontend" in item
                    }
                    transitions.append(
                        {"elapsed_s": elapsed, "roles": roles, "frontends": frontends}
                    )
                    await asyncio.to_thread(
                        output.write_text, json.dumps({"transitions": transitions}, indent=2)
                    )
                    settled = all(
                        bool(value["ready"])
                        if expected[name] == "elected"
                        else not value["running"]
                        for name, value in frontends.items()
                    )
                    if roles == expected and settled:
                        return elapsed
                    assert elapsed < bound, {"expected": expected, "observed": roles}
                    await asyncio.sleep(2)

            healthy = {name: "elected" if name == NAMES[0] else "standby" for name in NAMES}
            # Setup includes the 301 s missing-record guard, then Core's 45 s reclaim
            # confirmation. Neither interval belongs to the subsequent fault deadline.
            await wait_roles(healthy, 390)
            # Fault immediately after a genuine periodic publication; never renew offline.
            while True:
                snapshot = json.loads((root / "snapshot.json").read_text())
                issued = datetime.fromisoformat(snapshot["issued_at"].replace("Z", "+00:00"))
                if (datetime.now(UTC) - issued).total_seconds() < 3:
                    break
                await asyncio.sleep(0.5)
            workers[NAMES[0]].kill()
            await workers[NAMES[0]].wait()
            promotion = await wait_roles({NAMES[1]: "elected", NAMES[2]: "standby"}, 60)
            print(json.dumps({"Core_process_loss_promotion_s": promotion}), flush=True)
            # Restart only the owned fixture, retaining its real durable safety/journal files.
            workers[NAMES[0]] = await asyncio.create_subprocess_exec(
                sys.executable,
                SCRIPT,
                "--worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=logs[0],
            )
            workers[NAMES[0]].stdin.write(
                json.dumps({**initials[NAMES[0]], "resume": True}).encode() + b"\n"
            )
            await workers[NAMES[0]].stdin.drain()
            assert (await receive(NAMES[0]))["ready"]
            await rpc(NAMES[0], "arm")
            recovery = await wait_roles(healthy, 90)
            result = {
                "scope": "real-host signed participant HTTP; owned Core process SIGKILL/restart",
                "physical_power_loss": False,
                "frontend_lifecycle": bool(os.environ.get("COIRE_RESOURCE_FRONTEND_IMAGE")),
                "public_ingress": False,
                "promotion_s": promotion,
                "handback_s": recovery,
                "transitions": transitions,
                "source_identity": selected_source,
            }
            await asyncio.to_thread(output.write_text, json.dumps(result, indent=2))
            print(json.dumps({"Core_process_restart_handback_s": recovery}), flush=True)
            return
        for phase in ("baseline", "armed"):
            if phase == "armed":
                await rpc(NAMES[0], "arm")
                await asyncio.gather(*(rpc(name, "arm") for name in NAMES[1:]))
            start = time.monotonic()
            while True:
                samples = await asyncio.gather(*(rpc(name, "sample") for name in NAMES))
                for name, sample in zip(NAMES, samples, strict=True):
                    measurements[name].setdefault(phase, []).append(sample)
                await asyncio.to_thread(
                    output.write_text,
                    json.dumps(
                        {
                            "diagnostics": diagnostics,
                            "phase": phase,
                            "root": str(root),
                            "measurements": measurements,
                            "source_identity": selected_source,
                        },
                        indent=2,
                    ),
                )
                if time.monotonic() - start >= duration:
                    break
                await asyncio.sleep(10)
            print(json.dumps({"phase": phase, "seconds": time.monotonic() - start}), flush=True)
        summary = {}
        for name, windows in measurements.items():
            baseline, armed = windows["baseline"], windows["armed"]

            def cpu(values):
                return (
                    100
                    * (values[-1]["cpu_s"] - values[0]["cpu_s"])
                    / (values[-1]["at"] - values[0]["at"])
                )

            summary[name] = {
                "incremental_rss_mib": (
                    max(v["rss"] for v in armed) - statistics.mean(v["rss"] for v in baseline)
                )
                / 1024**2,
                "incremental_cpu_percent": cpu(armed) - cpu(baseline),
                "swap_growth_mib": max(v["swap_used_mib"] for v in armed)
                - baseline[0]["swap_used_mib"],
                "final_role": armed[-1]["role"],
            }
        await asyncio.to_thread(
            output.write_text,
            json.dumps(
                {
                    "diagnostics": diagnostics,
                    "phase": "complete",
                    "root": str(root),
                    "measurements": measurements,
                    "summary": summary,
                    "source_identity": selected_source,
                },
                indent=2,
            ),
        )
        print(json.dumps(summary, indent=2), flush=True)
        assert summary[NAMES[0]]["final_role"] == "elected"
        assert all(summary[name]["final_role"] == "standby" for name in NAMES[1:])
        assert all(item["incremental_rss_mib"] <= 32 for item in summary.values())
        assert all(item["incremental_cpu_percent"] <= 1 for item in summary.values())
        assert all(item["swap_growth_mib"] == 0 for item in summary.values())
    finally:
        for process in workers.values():
            if process.returncode is None:
                process.stdin.write(b'{"op":"exit"}\n')
                try:
                    await process.stdin.drain()
                    await asyncio.wait_for(process.wait(), timeout=30)
                except (TimeoutError, BrokenPipeError):
                    process.terminate()
                    await process.wait()
        for process in forwards:
            if process.returncode is None:
                process.terminate()
                await process.wait()
        log_stack.close()


if __name__ == "__main__":
    asyncio.run(worker() if "--worker" in sys.argv else run())
