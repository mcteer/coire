"""Owned Studio frontend for the operator-only participant fault trial."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from coire_core.settings import Settings
from coire_node.docker_api import DockerAPI


class FixtureDocker(DockerAPI):
    """Translate only the fixed service name so trials cannot touch another deployment."""

    def __init__(self, name: str) -> None:
        super().__init__(str(Path.home() / ".orbstack/run/docker.sock"))
        self.name = name

    def _owned_name(self, value: str) -> str:
        return self.name if value == "coire-failover" else value

    async def inspect_container(self, container_id: str) -> dict[str, Any] | None:
        return await super().inspect_container(self._owned_name(container_id))

    async def start_container(self, container_id: str) -> None:
        await super().start_container(self._owned_name(container_id))

    async def stop_container(self, container_id: str, *, timeout_s: int = 30) -> None:
        await super().stop_container(self._owned_name(container_id), timeout_s=timeout_s)


class FrontendContainer:
    def __init__(self, root: Path, settings: Settings, image: str) -> None:
        self.root = root
        self.settings = settings
        self.image = image
        self.docker = FixtureDocker(root.name + "-frontend")
        self.container: str | None = None
        self.network: str | None = None

    async def prepare(self) -> None:
        assert self.settings.failover_member_name in {"coire-edge-a", "coire-edge-b"}
        assert "@sha256:" in self.image
        assert await self.docker.inspect_container("coire-failover") is None, (
            "existing frontend must never be adopted or replaced by qualification"
        )
        assert await self.docker.inspect_image(self.image) is not None
        self.settings.failover_frontend_image = self.image
        mounts = [f"{self.root}:/opt/coire/failover:ro"]
        for field in ("failover_relay_token", "failover_monitor_token", "failover_lifecycle_token"):
            value = secrets.token_urlsafe(32)
            setattr(self.settings, field, SecretStr(value))
            path = self.root / field
            await asyncio.to_thread(path.write_text, value)
            await asyncio.to_thread(path.chmod, 0o600)
            # A single-file mount is readable by the non-root container, as in compose.
            # Keep these files outside the public snapshot mount.
            private = self.root.with_name(self.root.name + "-secrets")
            await asyncio.to_thread(private.mkdir, mode=0o700, exist_ok=True)
            destination = private / field
            await asyncio.to_thread(path.replace, destination)
            await asyncio.to_thread(destination.chmod, 0o644)
            mounts.append(f"{destination}:/run/secrets/{field}:ro")
        self.network = await self.docker.create_network(self.root.name, internal=True)
        self.container = await self.docker.create_container(
            self.docker.name,
            {
                "Image": self.image,
                "User": "65532:65532",
                "Labels": {"coire.qualification": self.root.name},
                "Env": [
                    "OTLP_ENDPOINT=http://127.0.0.1:1",
                    "FAILOVER_SNAPSHOT_PATH=/opt/coire/failover/snapshot.json",
                    "FAILOVER_PROOF_PATH=/opt/coire/failover/proof.json",
                    "FAILOVER_MEMBER_NAME=" + self.settings.failover_member_name,
                    "FAILOVER_CORE_PUBLIC_KEY=" + self.settings.failover_core_public_key,
                ],
                "Healthcheck": {
                    "Test": [
                        "CMD",
                        "/app/.venv/bin/python3",
                        "-c",
                        "import socket; socket.create_connection(('127.0.0.1',8004),2).close()",
                    ],
                    "Interval": 5_000_000_000,
                    "Timeout": 3_000_000_000,
                },
                "HostConfig": {
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "Memory": 256 * 1024**2,
                    "NanoCpus": 500_000_000,
                    "PidsLimit": 64,
                    "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=32m"},
                    "Binds": mounts,
                    "NetworkMode": self.network,
                },
            },
        )

    async def observe(self) -> dict[str, object]:
        assert self.container is not None
        value = await self.docker.inspect_container(self.container)
        assert value and not value["State"]["OOMKilled"]
        result: dict[str, object] = {
            "image_id": value["Image"],
            "container_id": self.container,
            "running": value["State"]["Running"],
            "ready": False,
        }
        if result["running"]:
            # Probe inside the private test network; no host ingress is created.
            script = """import json, pathlib, urllib.request, urllib.error

def status(path, headers):
    try:
        request = urllib.request.Request('http://127.0.0.1:8004'+path, headers=headers)
        return urllib.request.urlopen(request, timeout=2).status
    except urllib.error.HTTPError as error:
        return error.code

token = pathlib.Path('/run/secrets/failover_monitor_token').read_text()
print(json.dumps({'ready': status('/ready', {'X-Coire-Failover-Monitor': token}),
                  'anonymous': status('/v1/models', {})}))
"""
            process = await asyncio.create_subprocess_exec(
                "/usr/local/bin/docker",
                "exec",
                self.container,
                "/app/.venv/bin/python3",
                "-c",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            if process.returncode == 0:
                codes = json.loads(stdout)
                assert codes["anonymous"] == 401
                result["ready"] = codes["ready"] == 200
                result["ready_status"] = codes["ready"]
                result["anonymous_refused"] = True
        return result

    async def close(self) -> None:
        if self.container is not None:
            await self.docker.remove_container(self.container)
        if self.network is not None:
            await self.docker.remove_network(self.network)
        await self.docker.close()
