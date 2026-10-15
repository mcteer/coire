"""Private Studio supervisor for the armed application/native upgrade matrix.

Uses an already installed isolated Source, ordinary prepared artifacts, the
installed independent guard and the installed production node entry point.
Configuration and credentials arrive on SSH stdin, never command-line arguments.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import httpx
from application_cleanup import close_exited_engines
from application_supervisor_control import start_supervisor, stop_supervisor
from frontend_container import FrontendContainer

from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    PreparedNativeRuntime,
    UpgradeMaintenance,
    UpgradeRelease,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import publish_prepared_release, verify_prepared_release
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.store import Store
from coire_node.upgrade_guard import _owned_native_process, _owned_node_process
from coire_node.upgrade_runtime import verify_prepared_runtime


async def main() -> None:
    initial: dict[str, Any] = json.loads(sys.stdin.readline())
    name = initial["name"]
    assert name in {"coire-edge-a", "coire-edge-b"}
    assert socket.gethostname().split(".")[0] == name
    root = Path(initial["root"])
    assert root.is_absolute() and await asyncio.to_thread(root.resolve, strict=True) == root
    assert root.parent == Path.home() / ".coire/qualification/020"
    assert root.name.startswith("coire-020-application-")
    details = await asyncio.to_thread(root.stat)
    assert details.st_uid == os.getuid() and details.st_mode & 0o077 == 0

    def verify_prior_maintenance() -> None:
        for state in root.glob("state*/upgrades"):
            prior_fence = PrivateUpgradeStore(state, create=False).read(
                "maintenance.json", UpgradeMaintenance
            )
            assert prior_fence is None or prior_fence.state == "released", (
                "native prefix retains maintenance; install an independent fresh Source"
            )

    await asyncio.to_thread(verify_prior_maintenance)
    source = UpgradeRelease.model_validate(initial["source"])
    records = PrivateUpgradeStore(root / "fixture-records")
    bootstrap = PrivateUpgradeStore(root / "upgrade-bootstrap", create=False)
    assert not bootstrap.path("claim.json").exists(), "Source must never have been activated"
    runtime = PrivateUpgradeStore(root / "envs/.identities", create=False).read(
        str(source.manifest.environment_id) + ".json", PreparedNativeRuntime
    )
    assert runtime is not None
    python = await asyncio.to_thread(
        verify_prepared_runtime, root / "envs", root / "runtimes", runtime
    )
    assert (root / "envs/current").resolve() == root / "envs" / runtime.environment_id
    original_request = {
        "prepared_artifact_id": source.prepared_artifact_id,
        "expected_sha256": source.manifest_sha256,
    }
    from coire_core.models.upgrades import UpgradeReleaseRegister

    assert (
        await asyncio.to_thread(
            verify_prepared_release,
            root / "upgrades/prepared",
            UpgradeReleaseRegister.model_validate(original_request),
        )
        == source
    )
    # Actor/registration metadata is outside the immutable manifest. The selected
    # runtime and its manifest hash stay identical when registered in a fresh Core.
    registered = source.model_copy(
        update={
            "release_id": uuid5(UUID(initial["owner"]), source.manifest_sha256 + initial["trial"]),
            "prepared_by": UUID(initial["owner"]),
            "prepared_artifact_id": "020-application-source-"
            + initial["trial"]
            + "-"
            + source.manifest_sha256[:12],
        }
    )
    artifacts = {
        key: root / "upgrades/prepared" / source.prepared_artifact_id / key
        for key in source.manifest.artifact_sha256s
    }
    request = await asyncio.to_thread(
        publish_prepared_release, root / "upgrades/prepared", registered, artifacts
    )
    records.write("application-source-" + initial["trial"] + ".json", registered, replace=False)
    print(
        json.dumps(
            {
                "source": registered.model_dump(mode="json"),
                "request": request.model_dump(mode="json"),
            }
        ),
        flush=True,
    )
    configuration = json.loads(await asyncio.to_thread(sys.stdin.readline))
    assert configuration["op"] == "start"
    environment = dict(os.environ)
    for field_name, field in Settings.model_fields.items():
        if isinstance(field.default, str) and field.default.startswith("/opt/coire/"):
            environment[field_name.upper()] = str(root / field.default.removeprefix("/opt/coire/"))
    environment.update(
        {str(key): str(value) for key, value in configuration["environment"].items()}
    )
    environment.update(
        NODE_NAME=name,
        UPGRADES_ENABLED="true",
        LEGACY_NETWORK_MODE="false",
        NODE_REGISTRATION_ENABLED="true",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        RUN_DOCKER_SOCKET=str(Path.home() / ".orbstack/run/docker.sock"),
        NODE_STATE_DIR=str(root / ("state-" + initial["trial"])),
    )
    frontend_root = root / ("coire-020-app-" + initial["trial"] + "-failover")
    environment["FAILOVER_SNAPSHOT_PATH"] = str(frontend_root / "snapshot.json")
    environment["FAILOVER_PROOF_PATH"] = str(frontend_root / "proof.json")
    environment["FAILOVER_SAFETY_PATH"] = str(root / "state" / (initial["trial"] + "-safety.json"))
    settings = Settings.model_validate(
        {
            key.lower(): value
            for key, value in environment.items()
            if key.lower() in Settings.model_fields
        }
    )
    assert Path(settings.node_state_dir) == root / ("state-" + initial["trial"])
    assert Path(settings.upgrade_environment_root) == root / "envs"
    assert settings.failover_member_name == name

    def prepare_model() -> None:
        approved = configuration["model"]
        origin = Store("/opt/coire/models")
        target = Store(settings.node_store_dir)
        assert origin.root != target.root and target.root == root / "models"
        manifest = origin.read_manifest(approved["slug"])
        assert manifest is not None and manifest.sha256() == approved["manifest_sha256"]
        assert not origin.verify_against(manifest.slug, manifest)
        target.ensure_root()
        destination = target.path_for(manifest.slug)
        if destination.exists():
            retained = target.read_manifest(manifest.slug)
            assert retained is not None and retained.sha256() == manifest.sha256()
            assert not destination.is_symlink() and destination.stat().st_uid == os.getuid()
        else:
            shutil.copytree(origin.path_for(manifest.slug), destination)
            target.write_manifest(manifest)
        assert not target.verify_against(manifest.slug, manifest)
        assert all(path.stat().st_nlink == 1 for _, path in target.iter_files(manifest.slug))

    await asyncio.to_thread(prepare_model)
    await asyncio.to_thread(frontend_root.mkdir, parents=True, exist_ok=True, mode=0o755)
    frontend = FrontendContainer(frontend_root, settings, configuration["frontend_image"])
    # The installed production controller owns this fixed service name. Refuse
    # any pre-existing container; only this fixture's new container may use it.
    frontend.docker.name = "coire-failover"
    await frontend.prepare()
    for key in ("failover_relay_token", "failover_monitor_token", "failover_lifecycle_token"):
        environment[key.upper()] = getattr(settings, key).get_secret_value()
    environment["FAILOVER_FRONTEND_IMAGE"] = configuration["frontend_image"]
    log_path = root / "log" / ("application-" + initial["trial"] + ".log")
    log = log_path.open("xb")
    log_path.chmod(0o600)
    children: list[subprocess.Popen[bytes]] = []
    stopped = False
    try:
        guard = await asyncio.to_thread(
            subprocess.Popen,
            [str(python), "-I", "-B", "-m", "coire_node.upgrade_guard"],
            env=environment,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
        children.append(guard)
        deadline = time.monotonic() + 120
        while True:
            status = bootstrap.read("status.json", NativeGuardStatus)
            if status is not None and status.ready and status.process.pid == guard.pid:
                break
            assert guard.poll() is None and time.monotonic() < deadline, (
                "installed guard startup failed"
            )
            await asyncio.sleep(0.25)
        node = await asyncio.to_thread(
            subprocess.Popen,
            [str(python), "-I", "-B", "-m", "coire_node"],
            env=environment,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
        children.append(node)
        async with httpx.AsyncClient(
            base_url=f"http://{name}.lab:{settings.node_listen_port}",
            headers={"Authorization": "Bearer " + settings.node_token.get_secret_value()},
            trust_env=False,
            timeout=3,
        ) as client:
            while True:
                assert node.poll() is None and time.monotonic() < deadline, (
                    "installed node startup failed"
                )
                try:
                    if (await client.get("/node/health")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.25)
            print(
                json.dumps({"ready": True, "runtime": runtime.model_dump(mode="json")}), flush=True
            )
            await asyncio.to_thread(start_supervisor, root, initial["trial"])
            while line := await asyncio.to_thread(sys.stdin.readline):
                command = json.loads(line)
                if command["op"] == "sample":
                    observation: dict[str, Any] = {"frontend": await frontend.observe()}
                    try:
                        response = await client.get("/node/engines")
                        observation["node_http_status"] = response.status_code
                        observation["engines"] = (
                            response.json() if response.status_code == 200 else None
                        )
                    except httpx.TransportError:
                        observation["node_http_status"] = None
                        observation["engines"] = None
                    print(json.dumps(observation), flush=True)
                elif command["op"] == "stop":
                    assert command["core_terminal_and_coordinators_stopped"] is True
                    await close_exited_engines(client, Store(settings.node_store_dir))
                    await asyncio.to_thread(stop_supervisor, root, initial["trial"])
                    current = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades").read(
                        "node-process.json", NativeNodeProcess
                    )
                    assert current is not None
                    assert current.selected.runtime == runtime, "rollback to exact Source first"
                    current_python = await asyncio.to_thread(
                        verify_prepared_runtime,
                        root / "envs",
                        root / "runtimes",
                        current.selected.runtime,
                    )
                    owned = _owned_node_process(current.process, current_python)
                    assert owned is not None
                    owned.terminate()
                    await asyncio.to_thread(owned.wait, 30)
                    live_guard = bootstrap.read("status.json", NativeGuardStatus)
                    assert live_guard is not None
                    owned_guard = _owned_native_process(
                        live_guard.process, python, "coire_node.upgrade_guard"
                    )
                    assert owned_guard is not None
                    owned_guard.terminate()
                    await asyncio.to_thread(owned_guard.wait, 30)
                    await frontend.close()
                    stopped = True
                    print(json.dumps({"stopped": True}), flush=True)
                    break
                else:
                    raise ValueError("unknown private supervisor operation")
        assert stopped, "retain Source and guard for recovery; no successful retirement requested"
    finally:
        log.close()
        # Accepted ownership is never cleared or inferred from elapsed time.
        # On failure, keep installed children and journals for normal recovery.
        for child in children:
            if child.poll() is not None:
                child.wait()


if __name__ == "__main__":
    asyncio.run(main())
