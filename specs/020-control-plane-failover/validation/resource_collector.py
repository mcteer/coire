"""Owned, loopback-only production collector for native resource qualification."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx


class ResourceCollector:
    def __init__(self, name: str, image: str, output: Path) -> None:
        assert "@sha256:" in image, "qualification requires a pinned collector image"
        self.name = name + "-otel"
        self.image = image
        self.output = output
        self.container: str | None = None
        self.network: str | None = None
        self.endpoint = ""
        self.port = ""
        self.scrape = ""

    async def command(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        assert process.returncode == 0, stderr.decode()[:1000]
        return stdout.decode().strip()

    async def prepare(self) -> None:
        self.network = await self.command(
            "network",
            "create",
            "--label",
            "coire.qualification=" + self.name,
            self.name,
        )
        self.container = await self.command(
            "run",
            "-d",
            "--name",
            self.name,
            "--label",
            "coire.qualification=" + self.name,
            "--network",
            self.network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "10001:10001",
            "--memory",
            "384m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=32m",
            "--publish",
            "127.0.0.1::4317",
            "--publish",
            "127.0.0.1::8889",
            self.image,
        )
        metadata = json.loads(await self.command("inspect", self.container))[0]
        assert metadata["Name"] == "/" + self.name
        assert metadata["Config"]["Labels"]["coire.qualification"] == self.name
        ports = metadata["NetworkSettings"]["Ports"]
        assert all(ports[key][0]["HostIp"] == "127.0.0.1" for key in ("4317/tcp", "8889/tcp"))
        self.port = ports["4317/tcp"][0]["HostPort"]
        self.endpoint = "http://127.0.0.1:" + self.port
        self.scrape = "http://127.0.0.1:" + ports["8889/tcp"][0]["HostPort"] + "/metrics"
        deadline = time.monotonic() + 30
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            while True:
                try:
                    response = await client.get(self.scrape)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, "private collector did not become ready"
                await asyncio.sleep(0.1)

    async def verify(self, hosts: tuple[str, ...], *, diagnostics: bool) -> dict[str, Any]:
        """Require actual participant exports, not only a listening collector socket."""
        deadline = time.monotonic() + 30
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            while True:
                response = await client.get(self.scrape)
                response.raise_for_status()
                seen = [
                    host
                    for host in hosts
                    if any(
                        line.startswith("coire_failover_participant_last_seen_seconds{")
                        and f'host="{host}"' in line
                        for line in response.text.splitlines()
                    )
                ]
                if len(seen) == len(hosts):
                    break
                assert time.monotonic() < deadline, "collector did not receive all participants"
                await asyncio.sleep(1)
        await asyncio.to_thread(
            Path(str(self.output) + ".prometheus.txt").write_text, response.text
        )
        # The shipped collector's bounded debug exporter reports accepted trace batches.
        logs = await self.logs()
        trace_batches = logs.count('"otelcol.signal": "traces"')
        if diagnostics:
            assert trace_batches > 0, "diagnostic traces did not reach the collector"
        return {"image": self.image, "hosts": seen, "trace_batches": trace_batches}

    async def logs(self) -> str:
        assert self.container is not None
        process = await asyncio.create_subprocess_exec(
            "docker",
            "logs",
            self.container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode == 0
        value = stdout.decode()
        await asyncio.to_thread(Path(str(self.output) + ".otel.log").write_text, value)
        return value

    async def close(self) -> None:
        if self.container:
            try:
                await self.logs()
            finally:
                metadata = json.loads(await self.command("inspect", self.container))[0]
                assert metadata["Config"]["Labels"]["coire.qualification"] == self.name
                await self.command("rm", "-f", self.container)
        if self.network:
            metadata = json.loads(await self.command("network", "inspect", self.network))[0]
            assert metadata["Labels"]["coire.qualification"] == self.name
            assert not metadata["Containers"]
            await self.command("network", "rm", self.network)
