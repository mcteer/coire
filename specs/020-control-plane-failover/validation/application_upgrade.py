"""Operator-only Core and native upgrade/restoration with armed native peers.

Run after resource measurement. Retires only the positively idle prepared 020
cohort and provisions a fresh cohort with scoped native
credentials before qualifying Source. Never targets the production project.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import select
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

import httpx
from fixture_cleanup import retire_completed_project
from resource_collector import ResourceCollector

sys.path.insert(0, "tests")
from upgrade_composed_fixtures import candidate_image, qualify_source, script

from coire_core.models.upgrades import (
    COHORT_ORDER,
    CoreBootstrapSeed,
    UpgradeRelease,
    UpgradeServiceSpec,
)
from coire_core.upgrade_journal import PrivateUpgradeStore

REPO = Path.cwd()
HOSTS = ("coire-edge-a", "coire-edge-b")
PORT = 19470


def command(*args: str, **kwargs: Any) -> str:
    completed = subprocess.run(
        args, capture_output=True, text=True, check=False, timeout=120, **kwargs
    )
    if completed.returncode:
        log = REPO / "data/qualification/020/20e4390d/application-command-errors.log"
        with log.open("a") as output:
            output.write(completed.stderr + "\n")
        log.chmod(0o600)
        raise AssertionError("command failed; inspect " + str(log))
    return cast(str, completed.stdout.strip())


def run_matrix(
    *,
    root: Path,
    env: dict[str, str],
    project: str,
    previous: dict[str, Any],
    native_sources: dict[str, Any],
    seed: CoreBootstrapSeed,
    client: httpx.Client,
    send: Callable[[str, dict[str, Any]], None],
    receive: Callable[..., dict[str, Any]],
    save: Callable[..., None],
    frozen: Path,
    document: dict[str, Any],
    owner: UUID,
    tags: list[str],
    observations: list[dict[str, Any]],
    compose: list[str],
    telemetry: ResourceCollector,
    resume_core: bool = False,
) -> None:
    def observe() -> dict[str, Any]:
        for host in HOSTS:
            send(host, {"op": "sample"})
        native = {host: receive(host, 15) for host in HOSTS}
        try:
            ready = (
                client.get(
                    "/failover/ready",
                    headers={"X-Coire-Failover-Monitor": previous["monitor"]},
                ).status_code
                == 200
            )
        except httpx.TransportError:
            ready = False
        assert int(ready) + sum(int(value["frontend"]["ready"]) for value in native.values()) <= 1
        value = {"at": time.monotonic(), "core_ready": ready, "native": native}
        observations.append(value)
        save("observations", observations)
        return value

    def settled(bound: int = 390) -> None:
        deadline = time.monotonic() + bound
        while True:
            value = observe()
            targets = client.get("/api/v1/admin/upgrades/targets")
            if (
                targets.status_code == 200
                and value["core_ready"]
                and all(item["readiness"] == "ready" for item in targets.json()["items"])
            ):
                return
            assert time.monotonic() < deadline, "armed native/Core Source did not settle"
            time.sleep(2)

    settled()
    print("Installed native Sources, independent guards and armed Core ready", flush=True)

    def fingerprint() -> str:
        digest_code = """
import asyncio,hashlib,json
from sqlalchemy import text
from coire_api.db import session_scope, init_engine
from coire_core.settings import Settings
async def main():
    init_engine(Settings())
    fields={
      'models':'id,slug,source,repo_id,manifest_sha256',
      'model_variants':'id,model_id,slug,byte_size,validated,harness_verified',
      'variant_copies':'id,variant_id,node_id,manifest_sha256,bytes,verified',
      'harness_evaluations':'id,variant_id,verdict,scores',
      'users':'id,email,role,active',
      'api_keys':'id,user_id,scopes,revoked_at',
    }
    retained={}
    async with session_scope() as session:
        for table,columns in fields.items():
            rows=(await session.execute(text('SELECT '+columns+' FROM '+table+' ORDER BY id'))).mappings().all()
            retained[table]=[dict(row) for row in rows]
    print(hashlib.sha256(json.dumps(retained,sort_keys=True,default=str).encode()).hexdigest())
asyncio.run(main())
"""
        selection = json.loads(
            (root / "state" / project / "upgrades/coordination/runtime-selection.json").read_text()
        )
        identity = next(
            item["container"]["container_id"]
            for item in selection["services"]
            if item["container"]["service"] == "coire-api"
        )
        metadata = json.loads(command("docker", "inspect", identity))[0]
        assert metadata["State"]["Running"]
        assert metadata["Config"]["Labels"]["com.docker.compose.project"] == project
        assert metadata["Config"]["Labels"]["com.docker.compose.service"] == "coire-api"
        return command(
            "docker", "exec", identity, "/app/.venv/bin/python3", "-c", digest_code
        ).splitlines()[-1]

    preserved = fingerprint()
    before = root / "results/registry-and-identities-before.json"
    if before.exists():
        assert json.loads(before.read_text())["sha256"] == preserved
    else:
        save("registry-and-identities-before", {"sha256": preserved})
    if resume_core:
        operations = [
            value["operation"]
            for path in (root / "results").glob("*.json")
            if isinstance(value := json.loads(path.read_text()), dict) and "operation" in value
        ]
        assert any(
            upgrade["phase"] == rollback["phase"] == "succeeded"
            and upgrade["kind"] == rollback["kind"] == "control_plane"
            and upgrade["action"] == "upgrade"
            and rollback["action"] == "rollback"
            and rollback["parent_job_id"] == upgrade["job_id"]
            and upgrade["source_sha256"] == rollback["target_sha256"] == seed.source.manifest_sha256
            and upgrade["target_sha256"] == rollback["source_sha256"]
            for upgrade in operations
            for rollback in operations
        ), "resume requires retained successful Core upgrade and exact manual rollback"

    def finish(job_id: str, expected: str) -> dict[str, Any]:
        deadline = time.monotonic() + 1200
        phases: list[str] = []
        while True:
            observe()
            try:
                response = client.get("/api/v1/admin/upgrades/" + job_id)
                if response.status_code == 200:
                    value = response.json()
                    if not phases or value["phase"] != phases[-1]:
                        phases.append(value["phase"])
                        print(
                            json.dumps({"job_id": job_id, "phase": value["phase"]}),
                            flush=True,
                        )
                    assert value["phase"] != "recovery_required", (
                        "retain owner and inspect recovery"
                    )
                    if value["finished_at"] is not None:
                        save(job_id, {"operation": value, "phases": phases})
                        assert value["phase"] == expected
                        return cast(dict[str, Any], value)
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "application operation exceeded captured budget"
            time.sleep(1)

    broken_done = resume_core and any(
        op["kind"] == "control_plane"
        and op["phase"] == "rolled_back"
        and op["source_sha256"] == seed.source.manifest_sha256
        for op in operations
    )
    core_cases = (() if broken_done else (True,)) if resume_core else (False, True)
    for broken in core_cases:
        image, tag = candidate_image(
            document["services"]["coire-api"]["image"],
            broken_entrypoint=broken,
        )
        tags.append(tag)
        candidate = json.loads(frozen.read_text())
        candidate["services"]["coire-api"]["image"] = image
        payload = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(payload).hexdigest()
        key = "application-" + digest[:16]
        release_store = PrivateUpgradeStore(root / "state" / project / "releases" / key)
        artifact = release_store.path("compose.json")
        artifact.write_bytes(payload)
        artifact.chmod(0o600)
        release_store.sync()
        services = tuple(
            UpgradeServiceSpec(
                service=name,
                image_id=candidate["services"][name]["image"],
                configuration=candidate["services"][name],
                configuration_sha256=hashlib.sha256(
                    json.dumps(
                        candidate["services"][name], sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest(),
            )
            for name in COHORT_ORDER
            if name in candidate["services"]
        )
        manifest = seed.source.manifest.model_copy(
            update={
                "version": "020-live-application-" + ("broken" if broken else "healthy"),
                "prepared_at": datetime.now(UTC),
                "services": services,
                "artifact_sha256s": {"compose.json": digest},
                "compose_sha256": digest,
                "host_release_key": key,
                "staged_bytes": len(payload),
                "compatibility": seed.source.manifest.compatibility.model_copy(
                    update={"source_sha256s": (seed.source.manifest_sha256,)}
                ),
            }
        )
        release = UpgradeRelease(
            release_id=uuid4(),
            prepared_by=owner,
            prepared_artifact_id=key,
            manifest=manifest,
            manifest_sha256=manifest.sha256(),
        )
        release_file = root / "preparation" / (key + ".json")
        release_file.write_text(release.model_dump_json())
        release_file.chmod(0o600)
        registered = json.loads(
            script(
                REPO,
                env,
                "prepare-upgrade",
                str(root / "catalog"),
                "--release-document",
                str(release_file),
                "--artifact",
                "compose.json=" + str(artifact),
                "--publisher-image",
                seed.executor_image_id,
            )
        )
        assert client.post("/api/v1/admin/upgrades/releases", json=registered).status_code == 201
        response = client.post(
            "/api/v1/admin/upgrades/control-plane",
            json={
                "release_id": str(release.release_id),
                "expected_source_sha256": seed.source.manifest_sha256,
                "idempotency_key": str(uuid4()),
                "acknowledge_maintenance": True,
            },
        )
        assert response.status_code == 202, (response.status_code, response.text[:500])
        accepted = response.json()

        finished = finish(accepted["job_id"], "rolled_back" if broken else "succeeded")
        settled(120)
        if not broken:
            response = client.post(
                "/api/v1/admin/upgrades/" + finished["job_id"] + "/rollback",
                json={
                    "expected_source_sha256": release.manifest_sha256,
                    "idempotency_key": str(uuid4()),
                    "acknowledge_maintenance": True,
                },
            )
            assert response.status_code == 202
            finish(response.json()["job_id"], "succeeded")
        settled(120)
        assert fingerprint() == preserved, "immutable registry/account data changed"

    # A real installed native replacement through the same admin owner.
    # Only the neutral wheel modifier is reused from CI; hosted-only
    # provisioning/activation functions are never called on the lab.
    host = HOSTS[0]
    inputs = [
        str(path.relative_to(REPO))
        for package in ("coire_core", "coire_node")
        for path in (REPO / "dist").glob(package + "-*.whl")
    ]
    inputs.extend(
        [
            "dist/node-wheels/",
            "apps/coire-node/install.sh",
            "apps/coire-node/install_runtime.py",
            "tests/integration/upgrade_native_ci_wheels.py",
            "tests/integration/provision_upgrade_ci_vm.py",
            "specs/020-control-plane-failover/validation/native_candidate.py",
        ]
    )
    subprocess.run(
        ["rsync", "-azR", *inputs, host + ".lab:/tmp/coire-020-qualification/"],
        check=True,
        timeout=600,
    )
    native_source = UpgradeRelease.model_validate(
        json.loads((root / "results" / ("native-source-" + host + ".json")).read_text())["source"]
    )
    native_root = native_sources[host]["root"]
    for kind in ("healthy", "broken"):
        retained = root / "results" / ("native-target-" + kind + ".json")
        reuse_prepared = retained.exists()
        if reuse_prepared:
            prepared_native = json.loads(retained.read_text())
        else:
            stage_log = (root / "private" / ("stage-native-" + kind + ".log")).open("xb")
            try:
                staged = subprocess.run(
                    [
                        "ssh",
                        "-o",
                        "BatchMode=yes",
                        host + ".lab",
                        "cd /tmp/coire-020-qualification && .venv/bin/python specs/020-control-plane-failover/validation/native_candidate.py",
                    ],
                    input=json.dumps(
                        {
                            "root": native_root,
                            "state": str(Path(native_root) / ("state-" + project[-10:])),
                            "cohort": project[-10:],
                            "source": native_source.model_dump(mode="json"),
                            "owner": str(owner),
                            "trial": project[-10:] + "-" + kind,
                            "kind": kind,
                        }
                    ),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=stage_log,
                    check=True,
                    timeout=1500,
                )
            finally:
                stage_log.close()
            prepared_native = json.loads(staged.stdout)
        target = UpgradeRelease.model_validate(prepared_native["release"])
        save("native-target-" + kind, prepared_native)
        if reuse_prepared:
            registration = prepared_native["request"]
            assert registration == {
                "prepared_artifact_id": target.prepared_artifact_id,
                "expected_sha256": target.manifest_sha256,
            }
        else:
            local = root / "preparation" / ("native-" + kind)
            local.mkdir(mode=0o700, exist_ok=True)
            assert local.resolve(strict=True) == local and local.stat().st_uid == os.getuid()
            release_file = local / "release.json"
            if release_file.exists():
                assert not release_file.is_symlink()
                assert UpgradeRelease.model_validate_json(release_file.read_text()) == target
            else:
                release_file.write_text(target.model_dump_json())
            release_file.chmod(0o600)
            arguments = [
                "prepare-upgrade",
                str(root / "catalog"),
                "--release-document",
                str(release_file),
            ]
            for key, remote_file in prepared_native["files"].items():
                local_file = local / key
                if not local_file.exists():
                    command("scp", host + ".lab:" + remote_file, str(local_file))
                assert not local_file.is_symlink()
                assert (
                    hashlib.sha256(local_file.read_bytes()).hexdigest()
                    == target.manifest.artifact_sha256s[key]
                )
                arguments.extend(["--artifact", key + "=" + str(local_file)])
            arguments.extend(["--publisher-image", seed.executor_image_id])
            registration = json.loads(script(REPO, env, *arguments))
        assert client.post("/api/v1/admin/upgrades/releases", json=registration).status_code == 201
        response = client.post(
            "/api/v1/admin/upgrades/nodes/" + host,
            json={
                "release_id": str(target.release_id),
                "expected_source_sha256": native_source.manifest_sha256,
                "idempotency_key": str(uuid4()),
                "acknowledge_maintenance": True,
            },
        )
        assert response.status_code == 202, (response.status_code, response.text[:500])
        accepted = response.json()
        finished = finish(accepted["job_id"], "succeeded" if kind == "healthy" else "rolled_back")
        settled(120)
        if kind == "healthy":
            response = client.post(
                "/api/v1/admin/upgrades/" + finished["job_id"] + "/rollback",
                json={
                    "expected_source_sha256": target.manifest_sha256,
                    "idempotency_key": str(uuid4()),
                    "acknowledge_maintenance": True,
                },
            )
            assert response.status_code == 202
            finish(response.json()["job_id"], "succeeded")
        settled(120)
    after = fingerprint()
    assert after == preserved, "native transitions changed immutable registry/account data"
    save("registry-and-identities-after", {"sha256": after, "matches_original_baseline": True})
    save("telemetry", asyncio.run(telemetry.verify(HOSTS, diagnostics=False)))
    save(
        "outcome",
        {
            "operations_completed": True,
            "cleanup_complete": False,
            "scope": "armed Core/native upgrade, manual rollback and failed-candidate restoration; positive degraded inference and physical/public ingress remain separate",
        },
    )


def retire_matrix(
    *,
    root: Path,
    project: str,
    compose: list[str],
    peers: dict[str, subprocess.Popen[bytes]],
    send: Callable[[str, dict[str, Any]], None],
    receive: Callable[..., dict[str, Any]],
    save: Callable[..., None],
    tags: list[str],
    telemetry: ResourceCollector,
) -> None:
    # Retire all actual project coordinators before stopping native nodes.
    identities = command(
        "docker", "ps", "-q", "--filter", "label=com.docker.compose.project=" + project
    ).split()
    inspected = json.loads(command("docker", "inspect", *identities))
    coordinators = [
        item["Id"]
        for item in inspected
        if item["Config"]["Labels"].get("com.docker.compose.service")
        in {*COHORT_ORDER, "coire-upgrade-executor"}
    ]
    assert len(coordinators) == len(COHORT_ORDER) + 1
    command("docker", "stop", "--time=30", *coordinators)
    assert all(
        not item["State"]["Running"]
        for item in json.loads(command("docker", "inspect", *coordinators))
    )
    save("retired-coordinators", {"ids": coordinators})
    for name in HOSTS:
        send(name, {"op": "stop", "core_terminal_and_coordinators_stopped": True})
        assert receive(name)["stopped"]
        peers[name].wait(timeout=30)
        assert peers[name].returncode == 0
    command(*compose, "down", "--remove-orphans")
    save("docker-retirement", retire_completed_project(project, terminal=True))
    for tag in tags:
        command("docker", "image", "rm", tag)
    asyncio.run(telemetry.close())
    save(
        "outcome",
        {
            "completed": True,
            "operations_completed": True,
            "cleanup_complete": True,
            "scope": "armed Core/native upgrade, manual rollback, failed-candidate restoration; positive degraded inference and physical/public ingress remain separate",
        },
    )


def main() -> None:
    assert socket.gethostname().split(".")[0] == "coire-core"
    sequence = json.loads(
        (REPO / "data/qualification/020/20e4390d/resources-healthy-sequence.json").read_text()
    )
    assert sequence["stage"] == "complete" and sequence["lean_exit_code"] == 0, (
        "finish resource sampling first"
    )
    assert sequence["diagnostic"]["qualified"] and sequence["lean"]["qualified"]
    old_root = Path.home() / ".coire/qualification/020/coire-020-application-35c13da42b"
    previous = json.loads((old_root / "private/configuration.json").read_text())
    previous_seed = CoreBootstrapSeed.model_validate(previous["source"])
    old_frozen = old_root / "state" / previous["project"] / "current/compose.json"
    old_document = json.loads(old_frozen.read_text())
    assert old_document["name"] == previous["project"]
    assert previous["project"].startswith("coire-020-application-")
    native_sources = json.loads(Path(os.environ["COIRE_020_NATIVE_SOURCES"]).read_text())
    assert set(native_sources) == set(HOSTS)
    for host in HOSTS:
        native = native_sources[host]
        for key, package in (("coire-core.whl", "coire_core"), ("coire-node.whl", "coire_node")):
            (wheel,) = (REPO / "dist").glob(package + "-*.whl")
            assert (
                hashlib.sha256(wheel.read_bytes()).hexdigest()
                == native["source"]["manifest"]["artifact_sha256s"][key]
            )
    retirement_path = old_root / "results/retirement.json"
    if retirement_path.exists():
        retirement = json.loads(retirement_path.read_text())
        assert retirement["project"] == previous["project"]
        assert retirement["source_sha256"] == previous_seed.source.manifest_sha256
        assert retirement["running_containers"] == 0 and retirement["volumes_retained"]
        assert not command(
            "docker",
            "ps",
            "-q",
            "--filter",
            "label=com.docker.compose.project=" + previous["project"],
        )
    else:
        with httpx.Client(
            base_url=f"http://coire-core.lab:{PORT}",
            headers={"Authorization": "Bearer " + previous["admin_key"]},
            trust_env=False,
            timeout=10,
        ) as old:
            response = old.get("/api/v1/admin/upgrades/targets")
            response.raise_for_status()
            observed = next(
                item for item in response.json()["items"] if item["kind"] == "control_plane"
            )
            assert observed["readiness"] == "ready" and observed["bootstrap_ready"]
            assert observed["observed_sha256"] == previous_seed.source.manifest_sha256
        old_compose = [
            "docker",
            "compose",
            "-p",
            previous["project"],
            "-f",
            str(old_frozen),
            "--profile",
            "upgrades",
        ]
        # The prepared cohort has no accepted operation. Positively verify SQL too;
        # missing/unknown work never authorizes fixture retirement.
        counts = command(
            *old_compose,
            "exec",
            "-T",
            "coire-api",
            "/app/.venv/bin/python3",
            "-c",
            """
import asyncio
from sqlalchemy import text
from coire_api.db import session_scope, init_engine
from coire_core.settings import Settings
async def main():
    init_engine(Settings())
    async with session_scope() as session:
        for table in ('upgrade_operations', 'upgrade_maintenance'):
            value = await session.scalar(text('SELECT count(*) FROM ' + table))
            assert value == 0, 'prepared fixture has accepted ownership'
    print('idle')
asyncio.run(main())
""",
        )
        assert counts.splitlines()[-1] == "idle"
        command(*old_compose, "down", "--remove-orphans")
        retire_completed_project(previous["project"], terminal=True)
        assert not command(
            "docker",
            "ps",
            "-q",
            "--filter",
            "label=com.docker.compose.project=" + previous["project"],
        )
    retained_owner = json.loads(
        (
            old_root / "state" / previous["project"] / "upgrades/coordination/ownership.json"
        ).read_text()
    )
    assert (
        retained_owner["state"] == "committed" and retained_owner["project"] == previous["project"]
    )
    project = "coire-020-application-" + uuid4().hex[:10]
    root = Path.home() / ".coire/qualification/020" / project
    root.mkdir(mode=0o700)
    for directory in ("private", "results", "preparation"):
        (root / directory).mkdir(mode=0o700)
    overlay = root / "overlay.yaml"
    overlay.write_bytes((old_root / "overlay.yaml").read_bytes())
    api_image = os.environ["COIRE_020_API_IMAGE"]
    assert api_image.startswith("sha256:") and len(api_image) == 71
    overlay.write_text(
        overlay.read_text().replace(
            "  coire-api:\n", "  coire-api:\n    image: " + api_image + "\n"
        )
    )
    node_tokens = {name: secrets.token_urlsafe(32) for name in HOSTS}
    snapshot = json.loads(
        (old_root.parent / "application-preparation/registry-snapshot.json").read_text()
    )
    env = {
        **os.environ,
        **previous["env"],
        "PATH": "/usr/local/bin:/opt/homebrew/bin:" + os.environ["PATH"],
        "COMPOSE_FILE": str(REPO / "deploy/compose/compose.yaml") + os.pathsep + str(overlay),
        "COMPOSE_PROJECT_NAME": project,
        "COMPOSE_PROFILES": "upgrades,chat-files,mcp,ops",
        "COIRE_OPS_MODEL_ID": str(snapshot["model_variants"][0]["model_id"]),
        "COIRE_OPS_ENABLED": "true",
        "COIRE_MCP_ENABLED": "true",
        "COIRE_STATE_ROOT": str(root / "state"),
        "COIRE_SECRETS_BASE": str(root / "private"),
        "COIRE_SECRETS_DIR": str(root / "private"),
        "COIRE_SECRET_NODE_TOKENS": json.dumps(node_tokens),
    }
    private: dict[str, Any] = {
        "project": project,
        "root": str(root),
        "env": env,
        "previous": str(old_root),
        "node_tokens": node_tokens,
    }

    def save(name: str, value: Any, *, confidential: bool = False) -> None:
        path = root / ("private" if confidential else "results") / (name + ".json")
        path.write_text(json.dumps(value, indent=2) + "\n")
        path.chmod(0o600)

    save("configuration", private, confidential=True)
    peers: dict[str, subprocess.Popen[bytes]] = {}
    logs: list[BinaryIO] = []
    observations: list[dict[str, Any]] = []
    tags: list[str] = []
    compose: list[str] | None = None
    complete = False
    telemetry = ResourceCollector(
        project,
        "coire-otel@sha256:75098befa4eaff33bce99186313c625f391052392b7a6edf0319ed7482995347",
        root / "results/telemetry",
    )

    def receive(name: str, timeout: int = 180) -> dict[str, Any]:
        peer = peers[name]
        assert peer.stdout is not None
        assert select.select([peer.stdout], [], [], timeout)[0], "private native observer timed out"
        line = peer.stdout.readline()
        assert line, "private native observer exited; inspect retained log"
        return cast(dict[str, Any], json.loads(line))

    def send(name: str, value: dict[str, Any]) -> None:
        peer = peers[name]
        assert peer.stdin is not None
        peer.stdin.write(json.dumps(value).encode() + b"\n")
        peer.stdin.flush()

    try:
        asyncio.run(telemetry.prepare())
        with (root / "private/install.log").open("xb") as log:
            subprocess.run(
                [str(REPO / "deploy/compose/coire-up"), "--secrets-from-env", "--no-build"],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=300,
            )
        frozen = root / "state" / project / "current/compose.json"
        document = json.loads(frozen.read_text())
        compose = [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(frozen),
            "--profile",
            "upgrades",
            "--profile",
            "chat-files",
            "--profile",
            "mcp",
            "--profile",
            "ops",
        ]
        snapshot = json.loads(
            (old_root.parent / "application-preparation/registry-snapshot.json").read_text()
        )
        # Disposable fixture data only: retain acquired/verified immutable model
        # identities rather than pulling or manufacturing validation results.
        seed_code = """
import asyncio,json,sys
from sqlalchemy import text
from coire_api.db import session_scope, init_engine
from coire_core.settings import Settings
async def main():
    init_engine(Settings())
    data=json.load(sys.stdin)
    async with session_scope() as session:
        for table in ('nodes','models','model_variants','variant_copies','harness_evaluations'):
            for row in data[table]:
                await session.execute(text('INSERT INTO '+table+' SELECT * FROM json_populate_record(NULL::'+table+', CAST(:record AS json))'), {'record':json.dumps(row)})
        await session.commit()
    print('restored')
asyncio.run(main())
"""
        assert (
            command(
                *compose,
                "exec",
                "-T",
                "coire-api",
                "/app/.venv/bin/python3",
                "-c",
                seed_code,
                input=json.dumps(snapshot),
            ).splitlines()[-1]
            == "restored"
        )
        with httpx.Client(
            base_url=f"http://coire-core.lab:{PORT}", trust_env=False, timeout=10
        ) as client:
            bootstrap_headers = {"Authorization": "Bearer " + env["COIRE_SECRET_ADMIN_TOKEN"]}
            response = client.post(
                "/api/v1/admin/users",
                headers=bootstrap_headers,
                json={
                    "email": "application-upgrade@isolated.test",
                    "display_name": "020 live application validation",
                    "role": "admin",
                },
            )
            assert response.status_code == 201
            owner = UUID(response.json()["id"])
            response = client.post(
                f"/api/v1/admin/users/{owner}/keys",
                headers=bootstrap_headers,
                json={
                    "name": "020-application",
                    "scopes": ["admin"],
                    "requests_per_minute": 10000,
                    "monthly_budget_tokens": 10000,
                },
            )
            assert response.status_code == 201
            headers = {"Authorization": "Bearer " + response.json()["secret"]}
            client.headers.update(headers)
            private["admin_key"] = response.json()["secret"]
            private["owner"] = str(owner)
            private["registration_tokens"] = {}
            save("configuration", private, confidential=True)
            for name in HOSTS:
                native = native_sources[name]
                log = (root / "private" / (name + ".log")).open("xb")
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
                        "cd /tmp/coire-020-qualification && .venv/bin/python specs/020-control-plane-failover/validation/application_node.py",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=log,
                )
                send(
                    name,
                    {
                        "name": name,
                        "root": native["root"],
                        "source": native["source"],
                        "owner": str(owner),
                        "trial": project[-10:],
                    },
                )
                prepared = receive(name)
                release = UpgradeRelease.model_validate(prepared["source"])
                local_release = root / "preparation" / (name + "-source.json")
                local_release.write_text(release.model_dump_json())
                local_release.chmod(0o600)
                args = [
                    "prepare-upgrade",
                    str(root / "catalog"),
                    "--release-document",
                    str(local_release),
                ]
                for key, package in (
                    ("coire-core.whl", "coire_core"),
                    ("coire-node.whl", "coire_node"),
                ):
                    (wheel,) = (REPO / "dist").glob(package + "-*.whl")
                    assert (
                        hashlib.sha256(wheel.read_bytes()).hexdigest()
                        == release.manifest.artifact_sha256s[key]
                    )
                    args.extend(["--artifact", key + "=" + str(wheel)])
                args.extend(
                    ["--publisher-image", document["services"]["coire-upgrade-executor"]["image"]]
                )
                registration = json.loads(script(REPO, env, *args))
                registered = client.post("/api/v1/admin/upgrades/releases", json=registration)
                assert registered.status_code == 201
                original_node = next(node for node in snapshot["nodes"] if node["name"] == name)
                issued = client.post(
                    f"/api/v1/admin/nodes/{original_node['id']}/registration-token"
                )
                assert issued.status_code == 201
                private["registration_tokens"][name] = issued.json()["token"]
                save("configuration", private, confidential=True)
                save("native-source-" + name, prepared)
                model = snapshot["model_variants"][0]
                copy = next(
                    row
                    for row in snapshot["variant_copies"]
                    if row["node_id"] == original_node["id"]
                )
                send(
                    name,
                    {
                        "op": "start",
                        "frontend_image": previous["frontend_image"],
                        "model": {
                            "slug": model["slug"],
                            "manifest_sha256": copy["manifest_sha256"],
                        },
                        "environment": {
                            "NODE_TOKEN": node_tokens[name],
                            "NODE_REGISTRATION_TOKEN": issued.json()["token"],
                            "NODE_CONTROL_HOST": name + ".lab",
                            "UPGRADE_PROJECT": project,
                            "CORE_API_PORT": PORT,
                            "TRAINING_INPUT_API_URL": f"http://coire-core.lab:{PORT}",
                            "NODE_LISTEN_PORT": PORT,
                            "NODE_DATA_LISTEN_PORT": 19471,
                            "NODE_ENGINE_PORT_RANGE": "19500-19509",
                            "FAILOVER_MEMBER_NAME": name,
                            "FAILOVER_PEER_KEY": previous["keys"][name],
                            "FAILOVER_CORE_PUBLIC_KEY": previous["public"]["coire-core"],
                            "DIAGNOSTICS_ENABLED": "false",
                            "OTLP_ENDPOINT": f"unix:/tmp/coire-020-app-{project[-10:]}-otel.sock",
                        },
                    },
                )
                assert receive(name)["ready"]
            seed = qualify_source(REPO, root, env, frozen, client, headers, owner)
            private["source"] = seed.source.model_dump(mode="json")
            save("configuration", private, confidential=True)

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
                owner=owner,
                tags=tags,
                observations=observations,
                compose=compose,
                telemetry=telemetry,
            )
            complete = True
    finally:
        if complete and compose:
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
        for retained_log in logs:
            retained_log.close()
        if not complete:
            save(
                "outcome",
                {"completed": False, "retained_for_recovery": True, "collector": telemetry.name},
            )
        print("Private evidence: " + str(root), flush=True)


if __name__ == "__main__":
    main()
