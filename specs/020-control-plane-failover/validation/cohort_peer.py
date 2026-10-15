"""Owned native participant for real Core cohort upgrade acceptance.

Configuration arrives over private SSH stdin. The runner binds only its Studio's
control address, uses normal signed HTTP exchanges and never starts an engine.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from typing import Any

import uvicorn
from frontend_container import FrontendContainer
from pydantic import SecretStr

from coire_core.models.node import NetworkPath
from coire_node.failover.poller import StudioFailoverController
from coire_node.testing.harness import Agent


async def main() -> None:
    initial: dict[str, Any] = json.loads(sys.stdin.readline())
    name = initial["name"]
    assert name in {"coire-edge-a", "coire-edge-b"}
    assert socket.gethostname().split(".")[0] == name
    root = Path(initial["root"])
    assert root.is_absolute() and root.name.startswith("coire-020-cohort-")
    await asyncio.to_thread(root.mkdir, mode=0o700)
    frontend_root = root / (root.name + "-state")
    await asyncio.to_thread(frontend_root.mkdir, mode=0o755)
    agent = Agent(root / "agent", node_name=name)
    settings = agent.settings
    settings.failover_member_name = name
    settings.failover_peer_key = SecretStr(initial["key"])
    settings.failover_core_public_key = initial["core_public_key"]
    settings.failover_snapshot_path = str(frontend_root / "snapshot.json")
    settings.failover_proof_path = str(frontend_root / "proof.json")
    settings.failover_safety_path = str(root / "safety.json")
    settings.core_api_port = initial["port"]
    settings.node_listen_port = initial["port"]
    frontend = FrontendContainer(frontend_root, settings, initial["image"])
    controller = StudioFailoverController(settings, docker=frontend.docker)
    address = socket.gethostbyname(name + ".lab")
    assert address == {"coire-edge-a": "192.168.4.11", "coire-edge-b": "192.168.4.12"}[name]
    server = uvicorn.Server(
        uvicorn.Config(
            agent.app(NetworkPath.CONTROL),
            host=address,
            port=initial["port"],
            lifespan="off",
            log_level="critical",
        )
    )
    serving = None
    try:
        await frontend.prepare()
        serving = asyncio.create_task(server.serve())
        deadline = asyncio.get_running_loop().time() + 10
        while not server.started:
            assert not serving.done()
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.1)
        await controller.start()
        print(json.dumps({"ready": True}), flush=True)
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            if command["op"] == "exit":
                break
            assert command["op"] == "sample"
            member = controller._poller.participant if controller._poller else None
            print(
                json.dumps(
                    {
                        "member": name,
                        "role": member.role.value if member else "unarmed",
                        "serving": member.serving() if member else False,
                        "frontend": await frontend.observe(),
                        "engine_count": len(agent.engines.statuses()),
                    }
                ),
                flush=True,
            )
    finally:
        try:
            await controller.stop()
        finally:
            try:
                await frontend.close()
            finally:
                server.should_exit = True
                if serving:
                    await asyncio.wait_for(serving, 10)
                await asyncio.to_thread(agent.close)


if __name__ == "__main__":
    asyncio.run(main())
