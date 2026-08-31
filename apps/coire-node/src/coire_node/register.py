"""Node registration (T057).

The agent registers itself with the control plane at startup and re-registers periodically.
It never exits on failure: a node whose control plane is down must keep trying, because the
alternative is a node that silently stays out of the cluster after a transient outage (spec
edge case: "starts before the network is up after a reboot").
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import socket
import subprocess

import psutil

from coire_core.models.node import NodeEndpointSet, NodeRegistration, NodeRegistrationV2
from coire_core.net import ControlClient, MeshClient
from coire_core.settings import Settings

logger = logging.getLogger(__name__)

BACKOFF_INITIAL_S = 1.0
BACKOFF_MAX_S = 60.0
_GPU_CORES_RE = re.compile(r"Total Number of Cores:\s*(\d+)")


def read_gpu_cores() -> int | None:
    """GPU core count, read once at startup. None when it cannot be determined."""
    sp = shutil.which("system_profiler")
    if sp is None:
        return None
    try:
        out = subprocess.run([sp, "SPDisplaysDataType"], capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError):
        return None
    match = _GPU_CORES_RE.search(out.stdout)
    return int(match.group(1)) if match else None


def build_registration(
    settings: Settings, mesh_address: str, egress_address: str | None
) -> NodeRegistration:
    return NodeRegistration(
        name=settings.node_name or socket.gethostname().split(".")[0],
        token=settings.node_token,
        mesh_address=mesh_address,  # type: ignore[arg-type]
        egress_address=egress_address,  # type: ignore[arg-type]
        memory_total_bytes=psutil.virtual_memory().total,
        disk_total_bytes=psutil.disk_usage("/").total,
        gpu_cores=read_gpu_cores(),
        agent_version=settings.service_version,
    )


def build_registration_v2(settings: Settings) -> NodeRegistrationV2:
    name = settings.node_name or socket.gethostname().split(".")[0]
    return NodeRegistrationV2(
        name=name,
        token=settings.node_token,
        endpoints=NodeEndpointSet(
            control_host=settings.node_control_host or name,
            data_host=settings.node_data_host or f"{name}.fabric",
        ),
        memory_total_bytes=psutil.virtual_memory().total,
        disk_total_bytes=psutil.disk_usage("/").total,
        gpu_cores=read_gpu_cores(),
        agent_version=settings.service_version,
    )


class Registrar:
    """Registers with the control plane and keeps re-registering."""

    def __init__(
        self, settings: Settings, registration: NodeRegistration | NodeRegistrationV2
    ) -> None:
        self._settings = settings
        self._registration = registration
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="registrar")

    async def stop(self) -> None:
        self._stopping.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def register_once(self, client: MeshClient | ControlClient) -> bool:
        """One attempt. Returns True on success; never raises."""
        try:
            resp = await client.post(
                self._settings.core_mesh_host
                if self._settings.legacy_network_mode
                else self._settings.core_control_host,
                "/api/v1/nodes/register",
                port=self._settings.core_api_port,
                json=self._registration.model_dump(mode="json")
                | {"token": self._registration.token.get_secret_value()},
            )
        except Exception as exc:
            logger.warning("registration attempt failed: %s", exc)
            return False

        if resp.status_code == 200:
            logger.info("registered with the control plane as %s", self._registration.name)
            return True
        if resp.status_code in (401, 403):
            # Not retryable by waiting, but do not exit: an admin may fix the inventory or
            # the token while the agent is running.
            logger.error(
                "registration refused (HTTP %d): %s — check deploy/cluster/nodes.yaml and the "
                "node token in the System keychain",
                resp.status_code,
                resp.text[:200],
            )
            return False
        logger.warning("registration returned HTTP %d", resp.status_code)
        return False

    async def _run(self) -> None:
        backoff = BACKOFF_INITIAL_S
        interval = self._settings.node_probe_interval_s * 6
        client_type = MeshClient if self._settings.legacy_network_mode else ControlClient
        async with client_type(timeout=10.0) as client:
            while not self._stopping.is_set():
                ok = await self.register_once(client)
                delay = interval if ok else backoff
                backoff = BACKOFF_INITIAL_S if ok else min(backoff * 2, BACKOFF_MAX_S)
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                except TimeoutError:
                    continue
