"""Reattach read-only observations to an exact retained installed test Source."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

import httpx
from application_cleanup import close_exited_engines
from application_supervisor_control import start_supervisor, stop_supervisor
from frontend_container import FrontendContainer

from coire_core.models.upgrades import NativeGuardStatus, NativeNodeProcess, UpgradeRelease
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.store import Store
from coire_node.upgrade_guard import _owned_native_process, _owned_node_process
from coire_node.upgrade_runtime import verify_prepared_runtime


async def main() -> None:
    configuration = json.loads(sys.stdin.readline())
    root = Path(configuration["root"])
    name, trial = configuration["name"], configuration["trial"]
    assert name in {"coire-edge-a", "coire-edge-b"}
    assert socket.gethostname().split(".")[0] == name
    assert root.parent == Path.home() / ".coire/qualification/020"
    assert root.name.startswith("coire-020-application-")
    assert await asyncio.to_thread(root.resolve, strict=True) == root
    details = await asyncio.to_thread(root.stat)
    assert details.st_uid == os.getuid() and details.st_mode & 0o077 == 0
    source = UpgradeRelease.model_validate(configuration["source"])
    state = PrivateUpgradeStore(root / ("state-" + trial) / "upgrades", create=False)
    current = state.read("node-process.json", NativeNodeProcess)
    assert current is not None and current.selected.release_sha256 == source.manifest_sha256
    python = verify_prepared_runtime(root / "envs", root / "runtimes", current.selected.runtime)
    process = _owned_node_process(current.process, python)
    assert process is not None
    environment = process.environ()
    assert environment["UPGRADE_PROJECT"] == "coire-020-application-" + trial
    assert environment["NODE_NAME"] == name
    settings = Settings.model_validate(
        {
            key.lower(): value
            for key, value in environment.items()
            if key.lower() in Settings.model_fields
        }
    )
    frontend_root = root / ("coire-020-app-" + trial + "-failover")
    frontend = FrontendContainer(frontend_root, settings, environment["FAILOVER_FRONTEND_IMAGE"])
    frontend.docker.name = "coire-failover"
    metadata = await frontend.docker.inspect_container("coire-failover")
    assert (
        metadata is not None
        and metadata["Config"]["Labels"]["coire.qualification"] == frontend_root.name
    )
    frontend.container = metadata["Id"]
    (frontend.network,) = metadata["NetworkSettings"]["Networks"]
    bootstrap = PrivateUpgradeStore(root / "upgrade-bootstrap", create=False)
    async with httpx.AsyncClient(
        base_url=f"http://{name}.lab:{settings.node_listen_port}",
        headers={"Authorization": "Bearer " + settings.node_token.get_secret_value()},
        trust_env=False,
        timeout=3,
    ) as client:
        response = await client.get("/node/health")
        response.raise_for_status()
        await asyncio.to_thread(start_supervisor, root, trial)
        print(json.dumps({"ready": True, "resumed": True}), flush=True)
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            if command["op"] == "sample":
                value: dict[str, Any] = {"frontend": await frontend.observe()}
                try:
                    response = await client.get("/node/engines")
                    value["node_http_status"] = response.status_code
                    value["engines"] = response.json() if response.status_code == 200 else None
                except httpx.TransportError:
                    value.update(node_http_status=None, engines=None)
                print(json.dumps(value), flush=True)
            elif command["op"] == "stop":
                assert command["core_terminal_and_coordinators_stopped"] is True
                await close_exited_engines(client, Store(settings.node_store_dir))
                await asyncio.to_thread(stop_supervisor, root, trial)
                current = state.read("node-process.json", NativeNodeProcess)
                assert (
                    current is not None
                    and current.selected.release_sha256 == source.manifest_sha256
                )
                python = await asyncio.to_thread(
                    verify_prepared_runtime,
                    root / "envs",
                    root / "runtimes",
                    current.selected.runtime,
                )
                process = _owned_node_process(current.process, python)
                assert process is not None
                status = bootstrap.read("status.json", NativeGuardStatus)
                assert status is not None
                guard_python = await asyncio.to_thread(
                    verify_prepared_runtime,
                    root / "envs",
                    root / "runtimes",
                    status.bootstrap_runtime,
                )
                guard = _owned_native_process(
                    status.process, guard_python, "coire_node.upgrade_guard"
                )
                assert guard is not None
                process.terminate()
                await asyncio.to_thread(process.wait, 30)
                guard.terminate()
                await asyncio.to_thread(guard.wait, 30)
                await frontend.close()
                print(json.dumps({"stopped": True}), flush=True)
                return
            else:
                raise ValueError("unknown observer operation")
    # EOF never retires processes, clears holds or claims a successful run.
    await frontend.docker.close()


if __name__ == "__main__":
    asyncio.run(main())
