"""Resume remaining cases only after retained Core success and exact rollback."""

from __future__ import annotations

import json
import os
import select
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

import httpx
import psutil
from application_upgrade import HOSTS, PORT, command, retire_matrix, run_matrix
from resource_collector import ResourceCollector

from coire_core.models.upgrades import CoreBootstrapSeed


def main() -> None:
    root = Path(sys.argv[1])
    assert root.parent == Path.home() / ".coire/qualification/020"
    assert root.name.startswith("coire-020-application-")
    assert root.resolve(strict=True) == root and root.stat().st_uid == os.getuid()
    assert root.stat().st_mode & 0o077 == 0
    private = json.loads((root / "private/configuration.json").read_text())
    project, env = private["project"], private["env"]
    assert project == root.name
    previous = json.loads((Path(private["previous"]) / "private/configuration.json").read_text())
    native_sources = json.loads(Path(env["COIRE_020_NATIVE_SOURCES"]).read_text())
    frozen = root / "state" / project / "current/compose.json"
    document = json.loads(frozen.read_text())
    seed = CoreBootstrapSeed.model_validate_json((root / "preparation/seed.json").read_text())
    observations = json.loads((root / "results/observations.json").read_text())
    compose = ["docker", "compose", "-p", project, "-f", str(frozen)]
    for profile in ("upgrades", "chat-files", "mcp", "ops"):
        compose.extend(["--profile", profile])
    collector_image = (
        "coire-otel@sha256:75098befa4eaff33bce99186313c625f391052392b7a6edf0319ed7482995347"
    )
    telemetry = ResourceCollector(project, collector_image, root / "results/telemetry")
    metadata = json.loads(command("docker", "inspect", telemetry.name))[0]
    assert metadata["State"]["Running"]
    assert metadata["Config"]["Labels"]["coire.qualification"] == telemetry.name
    telemetry.container = metadata["Id"]
    (telemetry.network,) = metadata["NetworkSettings"]["Networks"]
    ports = metadata["NetworkSettings"]["Ports"]
    assert all(ports[p][0]["HostIp"] == "127.0.0.1" for p in ("4317/tcp", "8889/tcp"))
    telemetry.port = ports["4317/tcp"][0]["HostPort"]
    telemetry.endpoint = "http://127.0.0.1:" + telemetry.port
    telemetry.scrape = "http://127.0.0.1:" + ports["8889/tcp"][0]["HostPort"] + "/metrics"
    peers: dict[str, subprocess.Popen[bytes]] = {}
    logs: list[BinaryIO] = []
    tags: list[str] = []
    attempt = uuid4().hex[:10]
    complete = False

    def save(name: str, value: Any, **_kwargs: Any) -> None:
        path = root / "results" / (name + ".json")
        path.write_text(json.dumps(value, indent=2) + "\n")
        path.chmod(0o600)

    def send(name: str, value: dict[str, Any]) -> None:
        stream = peers[name].stdin
        assert stream is not None
        stream.write(json.dumps(value).encode() + b"\n")
        stream.flush()

    def receive(name: str, timeout: int = 180) -> dict[str, Any]:
        stream = peers[name].stdout
        assert stream is not None and select.select([stream], [], [], timeout)[0]
        line = stream.readline()
        assert line, "retained observer exited; inspect private log"
        return cast(dict[str, Any], json.loads(line))

    try:
        with httpx.Client(
            base_url=f"http://coire-core.lab:{PORT}",
            headers={"Authorization": "Bearer " + private["admin_key"]},
            trust_env=False,
            timeout=10,
        ) as client:
            targets = client.get("/api/v1/admin/upgrades/targets")
            targets.raise_for_status()
            assert all(
                v["readiness"] == "ready" and v["bootstrap_ready"] for v in targets.json()["items"]
            )
            core = next(v for v in targets.json()["items"] if v["kind"] == "control_plane")
            assert core["observed_sha256"] == seed.source.manifest_sha256
            selection = json.loads(
                (
                    root / "state" / project / "upgrades/coordination/runtime-selection.json"
                ).read_text()
            )
            api = next(
                v["container"]["container_id"]
                for v in selection["services"]
                if v["container"]["service"] == "coire-api"
            )
            code = """import asyncio
from sqlalchemy import text
from coire_api.db import init_engine,session_scope
from coire_core.settings import Settings
async def main():
 init_engine(Settings())
 async with session_scope() as s:
  assert await s.scalar(text('select count(*) from upgrade_operations where active_slot=1'))==0
  assert await s.scalar(text('select count(*) from upgrade_maintenance where released=false'))==0
 print('terminal cleanup verified')
asyncio.run(main())
"""
            assert (
                command("docker", "exec", api, "/app/.venv/bin/python3", "-c", code).splitlines()[
                    -1
                ]
                == "terminal cleanup verified"
            )
            retired_observers = []
            for name in HOSTS:
                expected = [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-R",
                    f"/tmp/coire-020-app-{project[-10:]}-otel.sock:127.0.0.1:{telemetry.port}",
                    name + ".lab",
                    "cd /tmp/coire-020-qualification && .venv/bin/python specs/020-control-plane-failover/validation/application_node.py",
                ]
                for process in psutil.process_iter():
                    try:
                        actual = process.cmdline()
                        resumed = [
                            *expected[:-1],
                            expected[-1].replace("application_node.py", "application_observer.py"),
                        ]
                        if actual not in (expected, resumed):
                            continue
                        born = process.create_time()
                        assert process.ppid() == 1 and process.uids().real == os.getuid()
                        assert Path(process.exe()).resolve(strict=True) == Path("/usr/bin/ssh")
                        assert process.cmdline() == actual and process.create_time() == born
                        process.terminate()
                        process.wait(15)
                        retired_observers.append(
                            {"pid": process.pid, "create_time": born, "node": name}
                        )
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            save("retired-orphan-observers-" + attempt, retired_observers)
            for name in HOSTS:
                # An orphaned SSH socket is removed only with owned socket type and
                # positive ECONNREFUSED. A live listener is never replaced.
                cleanup = """import socket,stat,os,errno
from pathlib import Path
p=Path('/tmp/coire-020-app-TRIAL-otel.sock')
if p.exists() or p.is_symlink():
 s=p.lstat();assert stat.S_ISSOCK(s.st_mode) and s.st_uid==os.getuid()
 with socket.socket(socket.AF_UNIX) as c:
  c.settimeout(1)
  try:c.connect(str(p))
  except OSError as e:
   assert e.errno==errno.ECONNREFUSED
   p.unlink()
  else:raise RuntimeError('prior observer is live')
""".replace("TRIAL", project[-10:])
                command(
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    name + ".lab",
                    "cd /tmp/coire-020-qualification && .venv/bin/python -c "
                    + shlex.quote(cleanup),
                )
                log = (root / "private" / ("resume-" + attempt + "-" + name + ".log")).open("xb")
                logs.append(log)
                peers[name] = subprocess.Popen(
                    [
                        "ssh",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ExitOnForwardFailure=yes",
                        "-R",
                        f"/tmp/coire-020-app-{project[-10:]}-otel.sock:127.0.0.1:{telemetry.port}",
                        name + ".lab",
                        "cd /tmp/coire-020-qualification && .venv/bin/python specs/020-control-plane-failover/validation/application_observer.py",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=log,
                )
                send(
                    name,
                    {
                        "name": name,
                        "trial": project[-10:],
                        "root": native_sources[name]["root"],
                        "source": native_sources[name]["source"],
                    },
                )
                assert receive(name)["resumed"]
            save(
                "resume-" + attempt,
                {
                    "prior_core_success_and_cleanup_verified": True,
                    "no_authority_or_deadline_reset": True,
                },
            )
            run_matrix(
                root=root,
                env=env,
                project=project,
                previous=previous,
                native_sources=native_sources,
                seed=seed,
                client=client,
                send=send,
                receive=receive,
                save=save,
                frozen=frozen,
                document=document,
                owner=UUID(private["owner"]),
                tags=tags,
                observations=observations,
                compose=compose,
                telemetry=telemetry,
                resume_core=True,
            )
            complete = True
        retire_matrix(
            root=root,
            project=project,
            compose=compose,
            peers=peers,
            send=send,
            receive=receive,
            save=save,
            tags=tags,
            telemetry=telemetry,
        )
    finally:
        for retained_log in logs:
            retained_log.close()
        if not complete:
            save(
                "resume-" + attempt + "-outcome",
                {"completed": False, "retained_for_recovery": True},
            )
        print("Private evidence: " + str(root), flush=True)


if __name__ == "__main__":
    main()
