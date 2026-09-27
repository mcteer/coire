"""Automatically measure the Studio link when its versioned prerequisites change."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from pathlib import Path

import anyio
from sqlalchemy import select

from coire_api.audit import write_audit
from coire_api.db import LinkObservationRow, NodeRow, session_scope
from coire_api.nodes_client import NodeClient
from coire_api.sharding import append_observation
from coire_core.models import LinkProbeCommand, ProbeTransport
from coire_core.models.node import NodeStatus, NodeStatusV2, Reachability
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


def versions_need_probe(rows: list[LinkObservationRow], signature: tuple[str, str, str]) -> bool:
    current = {
        row.transport
        for row in rows
        if (row.os_version_a, row.os_version_b, row.engine_version) == signature
    }
    return current != {ProbeTransport.JACCL, ProbeTransport.RING}


class LinkProbeCoordinator:
    """Debounce health samples into first-boot and version-change probe cycles."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._versions: dict[str, tuple[str, str]] = {}
        self._attempted: tuple[str, str, str] | None = None
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="link-probe-coordinator")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task

    def observe(self, node: str, status: NodeStatus | NodeStatusV2) -> None:
        if node not in {"coire-edge-a", "coire-edge-b"}:
            return
        observed = (status.os_version, status.engine_version)
        if self._versions.get(node) != observed:
            self._versions[node] = observed
            self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                await self.pass_once()
            except Exception:
                logger.exception("automatic Studio link probe failed")
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.settings.node_probe_interval_s
                    )
                except TimeoutError:
                    self._wake.set()

    async def pass_once(self) -> None:
        if set(self._versions) != {"coire-edge-a", "coire-edge-b"}:
            return
        os_a, engine_a = self._versions["coire-edge-a"]
        os_b, engine_b = self._versions["coire-edge-b"]
        if engine_a != engine_b:
            logger.info(
                "deferring link probe while engine versions differ edge_a=%s edge_b=%s",
                engine_a,
                engine_b,
            )
            return
        signature = (os_a, os_b, engine_a)
        if self._attempted == signature:
            return
        async with session_scope() as session:
            healthy = set(
                (
                    await session.execute(
                        select(NodeRow.name).where(
                            NodeRow.name.in_(["coire-edge-a", "coire-edge-b"]),
                            NodeRow.reachability == Reachability.HEALTHY,
                        )
                    )
                ).scalars()
            )
            if healthy != {"coire-edge-a", "coire-edge-b"}:
                return
            latest = list(
                (
                    await session.execute(
                        select(LinkObservationRow)
                        .order_by(LinkObservationRow.observed_at.desc())
                        .limit(10)
                    )
                )
                .scalars()
                .all()
            )
            if not versions_need_probe(latest, signature):
                self._attempted = signature
                return
            commands: list[LinkProbeCommand] = []
            for transport, configured in (
                (ProbeTransport.JACCL, self.settings.sharding_jaccl_hostfile),
                (ProbeTransport.RING, self.settings.sharding_ring_hostfile),
            ):
                payload = await anyio.to_thread.run_sync(Path(configured).read_bytes)
                commands.append(
                    LinkProbeCommand(
                        command_id=uuid.uuid4(),
                        transport=transport,
                        hostfile_sha256=hashlib.sha256(payload).hexdigest(),
                    )
                )
            async with NodeClient(
                self.settings, timeout=self.settings.sharding_start_timeout_s
            ) as client:
                for command in commands:
                    observation = await client.run_link_probe("coire-edge-a", command)
                    await append_observation(session, observation)
            await write_audit(
                session,
                actor="system:link-probe-coordinator",
                action="link.probe.auto",
                target_type="studio_link",
                target_id="coire-edge-a:coire-edge-b",
                detail={"os_versions": [os_a, os_b], "engine_version": engine_a},
            )
            logger.info(
                "automatic Studio link probe completed os_a=%s os_b=%s engine_version=%s",
                os_a,
                os_b,
                engine_a,
            )
        # Only suppress another attempt once observations and audit have committed.
        self._attempted = signature
