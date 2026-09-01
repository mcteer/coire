"""Reconcile authoritative database runs with label-scoped Studio containers."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from opentelemetry import metrics, trace
from sqlalchemy import select

from coire_api.audit import write_audit
from coire_api.db import AgentRunRow, NodeRow, session_scope
from coire_api.nodes_client import NodeClient, NodeError
from coire_core.models.node import NodeRole
from coire_core.models.runs import TERMINAL_RUN_STATES, RunReconcileRequest
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.run_reconciler")
orphans_total = metrics.get_meter("coire.api.run_reconciler").create_counter(
    "coire_run_orphans_total", unit="1"
)


class RunReconciliationCoordinator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="run-reconciler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.reconcile_once()
            except Exception:
                logger.exception("run reconciliation pass failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.registry_reconcile_interval_s
                )

    async def reconcile_once(self) -> None:
        async with session_scope() as session:
            nodes = list(
                (
                    await session.scalars(select(NodeRow).where(NodeRow.role == NodeRole.STUDIO))
                ).all()
            )
            active = list(
                (
                    await session.scalars(
                        select(AgentRunRow).where(
                            AgentRunRow.state.notin_(list(TERMINAL_RUN_STATES))
                        )
                    )
                ).all()
            )
        by_node = {
            node.id: frozenset(run.id for run in active if run.node_id == node.id) for node in nodes
        }
        async with NodeClient(self.settings, timeout=5.0) as client:
            for node in nodes:
                try:
                    with tracer.start_as_current_span("coire.api.run.reconcile"):
                        observed = await client.reconcile_runs(
                            node.name,
                            RunReconcileRequest(
                                authoritative_run_ids=by_node[node.id], reap_orphans=False
                            ),
                        )
                except NodeError:
                    logger.warning("run reconciliation deferred node=%s", node.name)
                    continue
                if observed.orphan_run_ids:
                    # The first snapshot can race placement: a run may acquire its node and
                    # create a container after the database read but before the node observes
                    # it. Refresh authoritative state before any destructive request.
                    async with session_scope() as session:
                        refreshed = frozenset(
                            (
                                await session.scalars(
                                    select(AgentRunRow.id).where(
                                        AgentRunRow.node_id == node.id,
                                        AgentRunRow.state.notin_(list(TERMINAL_RUN_STATES)),
                                    )
                                )
                            ).all()
                        )
                    try:
                        result = await client.reconcile_runs(
                            node.name,
                            RunReconcileRequest(authoritative_run_ids=refreshed, reap_orphans=True),
                        )
                    except NodeError:
                        logger.warning("run orphan reap deferred node=%s", node.name)
                        continue
                    if not result.reaped_run_ids:
                        continue
                    orphans_total.add(len(result.reaped_run_ids), {"node": node.name})
                    async with session_scope() as session:
                        for run_id in result.reaped_run_ids:
                            await write_audit(
                                session,
                                actor="coire-run-reconciler",
                                action="agent_run.orphan_reaped",
                                target_type="agent_run",
                                target_id=str(run_id),
                                detail={"node": node.name},
                            )
