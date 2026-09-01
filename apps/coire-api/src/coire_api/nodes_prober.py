"""Background node prober (T058).

Probes each registered node over the control fabric and maintains `reachability`. Feature 000 sets only
HEALTHY / UNREACHABLE / UNKNOWN; the damped `degraded` state, hysteresis and health-record
freshness windows belong to feature 009.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coire_api.db import NodeRow, create_engine
from coire_api.health_evaluator import evaluate_probe
from coire_core.models.node import NodePath, NodeStatus, Reachability
from coire_core.net import ControlClient
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


class NodeProber:
    """Polls `/node/health` on every registered node and records what it finds."""

    def __init__(self, settings: Settings, reconciler: object | None = None) -> None:
        self._settings = settings
        self._reconciler = reconciler
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def set_reconciler(self, reconciler: object) -> None:
        self._reconciler = reconciler

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="node-prober")

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        engine = create_engine(self._settings)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            while not self._stopping.is_set():
                try:
                    await self._probe_once(maker)
                except Exception:
                    logger.exception("node probe cycle failed; will retry next interval")
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self._settings.node_probe_interval_s
                    )
                except TimeoutError:
                    continue
        finally:
            await engine.dispose()

    async def _probe_once(self, maker: async_sessionmaker[AsyncSession]) -> None:
        tokens = self._settings.node_token_map
        async with maker() as session:
            rows = list((await session.execute(select(NodeRow))).scalars().all())
            if not rows:
                return
            async with ControlClient(timeout=5.0) as client:
                for row in rows:
                    await self._probe_node(client, row, tokens.get(row.name, ""))
            await session.commit()

    async def _probe_node(self, client: ControlClient, row: NodeRow, token: str) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        started = time.perf_counter()
        observed: NodeStatus | None = None
        try:
            resp = await client.get(
                row.name,
                "/node/health",
                port=self._settings.node_listen_port,
                headers=headers,
            )
            ok = resp.status_code == 200
            if not ok:
                logger.warning("probe of %s returned HTTP %d", row.name, resp.status_code)
            else:
                raw = resp.json()
                if raw.get("path") == "control":
                    raw["path"] = NodePath.MESH.value
                observed = NodeStatus.model_validate(raw)
        except Exception as exc:
            logger.warning("probe of %s failed: %s", row.name, exc)
            ok = False

        latency_ms = (time.perf_counter() - started) * 1000
        decision = evaluate_probe(
            current=row.reachability,
            status=observed if ok else None,
            latency_ms=latency_ms if ok else None,
            failures=row.probe_failures,
            successes=row.probe_successes,
            degraded=row.probe_degraded,
            settings=self._settings,
        )
        old = row.reachability
        row.reachability = decision.verdict
        row.probe_failures = decision.failures
        row.probe_successes = decision.successes
        row.probe_degraded = decision.degraded
        row.heartbeat_latency_ms = latency_ms

        if ok and observed is not None:
            # Receipt time advances only for an actual observation. Updating it on a failed
            # attempt makes the previous payload look fresh while the node is offline.
            row.last_observed_at = datetime.now(UTC)
            row.last_observation = observed.model_dump(mode="json")
            row.last_seen_at = row.last_observed_at
            recovered = old is not Reachability.HEALTHY and row.reachability is Reachability.HEALTHY
            if recovered and self._reconciler is not None:
                # A node that has come back may be running something the registry does not
                # know about, or may have lost something it does (spec FR-015).
                logger.info("node %s recovered; requesting a reconcile", row.name)
                self._reconciler.request_reconcile(row.name)  # type: ignore[attr-defined]
            return
        if old is not Reachability.UNREACHABLE and row.reachability is Reachability.UNREACHABLE:
            logger.error("node %s unreachable after %d failures", row.name, row.probe_failures)
