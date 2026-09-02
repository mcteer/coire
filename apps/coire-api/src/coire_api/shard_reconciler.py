"""Conjunctive rank health, whole-group teardown, and bounded survivor fallback."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime

from opentelemetry import metrics, trace
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelVariantRow,
    NodeMemoryLedgerRow,
    NodeRow,
    ShardCommandRow,
    ShardGroupRow,
    VariantCopyRow,
    session_scope,
)
from coire_api.instance.service import append_initial_transition, transition
from coire_api.nodes_client import NodeClient, NodeError
from coire_core.models import InstanceState, MemoryReservationState, ShardGroupState
from coire_core.models.node import Reachability
from coire_core.settings import Settings

COMMAND_NAMESPACE = uuid.UUID("97f81cc1-853f-478d-961a-30a82e146d67")
tracer = trace.get_tracer("coire.api.sharding")
meter = metrics.get_meter("coire.api.sharding")
rank_failures = meter.create_counter("coire_sharding_rank_failures_total", unit="1")
fallbacks = meter.create_counter("coire_sharding_fallbacks_total", unit="1")
logger = logging.getLogger(__name__)


def _stop_id(group_id: uuid.UUID, node_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(COMMAND_NAMESPACE, f"{group_id}:{node_id}:stop")


class ShardReconciler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="shard-reconciler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.pass_once()
            except Exception:
                logger.exception("shard reconciliation pass failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), self.settings.node_engine_health_interval_s
                )

    async def pass_once(self) -> None:
        with tracer.start_as_current_span("coire.sharding.reconcile"):
            async with session_scope() as session:
                groups = list(
                    (
                        await session.execute(
                            select(ShardGroupRow).where(
                                ShardGroupRow.state == ShardGroupState.READY
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            for group in groups:
                await self._check_group(group.id)

    async def _check_group(self, group_id: uuid.UUID) -> None:
        async with session_scope() as session:
            group = await session.get(ShardGroupRow, group_id)
            if group is None:
                return
            members = list(
                (
                    await session.execute(
                        select(InstanceMemberRow, NodeRow)
                        .join(NodeRow, NodeRow.id == InstanceMemberRow.node_id)
                        .where(InstanceMemberRow.instance_id == group.instance_id)
                        .order_by(InstanceMemberRow.rank)
                    )
                ).all()
            )
        failed_node: NodeRow | None = None
        async with NodeClient(self.settings, timeout=10) as client:
            for _member, node in members:
                try:
                    status = await client.shard_group(node.name, group_id)
                    healthy = status.state is ShardGroupState.READY
                except NodeError:
                    healthy = False
                if not healthy:
                    failed_node = node
                    break
        if failed_node is None:
            async with session_scope() as session:
                rows = list(
                    (
                        await session.execute(
                            select(InstanceMemberRow).where(
                                InstanceMemberRow.instance_id == group.instance_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in rows:
                    row.rank_healthy = True
                    row.last_rank_health_at = datetime.now(UTC)
            return
        await self._fail_group(group_id, failed_node.id)

    async def _fail_group(self, group_id: uuid.UUID, failed_node_id: uuid.UUID) -> None:
        async with session_scope() as session:
            group = await session.get(ShardGroupRow, group_id)
            if group is None or group.state is not ShardGroupState.READY:
                return
            instance = await session.get(ModelInstanceRow, group.instance_id)
            members = list(
                (
                    await session.execute(
                        select(InstanceMemberRow).where(
                            InstanceMemberRow.instance_id == group.instance_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            # Keep the group in STOPPING until both rank stop commands are confirmed.  The
            # fallback launch gate uses this state to avoid starting a survivor while the
            # failed group's engine is still tearing down.
            group.state = ShardGroupState.STOPPING
            rank_failures.add(1, {"node_id": str(failed_node_id)})
            group.state_reason = "rank health conjunction failed"
            if instance is not None and instance.state is not InstanceState.FAILED:
                await transition(
                    session,
                    instance.id,
                    InstanceState.FAILED,
                    reason="a sharded rank was lost",
                    failure_code="rank_lost",
                )
            ledger = await session.get(NodeMemoryLedgerRow, failed_node_id)
            failed_node = await session.get(NodeRow, failed_node_id)
            if failed_node is not None:
                failed_node.reachability = Reachability.DEGRADED
            if ledger is not None:
                ledger.health = Reachability.DEGRADED
                ledger.health_reason = f"rank lost in shard group {group_id}"
                ledger.health_sampled_at = datetime.now(UTC)
            for member in members:
                member.rank_healthy = False
                command_id = _stop_id(group.id, member.node_id)
                if await session.get(ShardCommandRow, command_id) is None:
                    session.add(
                        ShardCommandRow(
                            id=command_id,
                            group_id=group.id,
                            node_id=member.node_id,
                            operation="stop",
                        )
                    )
            if instance is not None and instance.fallback_attempted_at is None:
                await self._create_fallback(session, instance, members, failed_node_id)
            logger.error(
                "shard rank lost group_id=%s instance_id=%s failed_node_id=%s",
                group_id,
                group.instance_id,
                failed_node_id,
            )

    async def _create_fallback(
        self,
        session: AsyncSession,
        failed: ModelInstanceRow,
        members: list[InstanceMemberRow],
        failed_node_id: uuid.UUID,
    ) -> None:
        failed.fallback_attempted_at = datetime.now(UTC)
        survivor = next((member for member in members if member.node_id != failed_node_id), None)
        if survivor is None:
            failed.fallback_no_fit = True
            return
        node = await session.get(NodeRow, survivor.node_id)
        ledger = await session.get(NodeMemoryLedgerRow, survivor.node_id)
        reserved = await session.scalar(
            select(func.coalesce(func.sum(MemoryReservationRow.bytes), 0)).where(
                MemoryReservationRow.node_id == survivor.node_id,
                MemoryReservationRow.state.in_(
                    [
                        MemoryReservationState.PENDING,
                        MemoryReservationState.HELD,
                        MemoryReservationState.RELEASING,
                    ]
                ),
            )
        )
        available = (ledger.budget_bytes if ledger else 0) - int(reserved or 0)
        variant = await session.scalar(
            select(ModelVariantRow)
            .join(
                VariantCopyRow,
                (VariantCopyRow.variant_id == ModelVariantRow.id)
                & (VariantCopyRow.node_id == survivor.node_id),
            )
            .where(
                ModelVariantRow.model_id == failed.model_id,
                ModelVariantRow.id != failed.variant_id,
                ModelVariantRow.validated.is_(True),
                ModelVariantRow.memory_estimate_bytes <= available,
                VariantCopyRow.verified.is_(True),
            )
            .order_by(ModelVariantRow.memory_estimate_bytes.desc())
            .limit(1)
        )
        if variant is None or node is None:
            failed.fallback_no_fit = True
            fallbacks.add(1, {"outcome": "no_fit"})
            logger.warning(
                "shard fallback has no fit instance_id=%s failed_node_id=%s",
                failed.id,
                failed_node_id,
            )
            return
        fallback = ModelInstanceRow(
            model_id=failed.model_id,
            variant_id=variant.id,
            policy=f"single:{node.name}",
            state=InstanceState.REQUESTED,
        )
        session.add(fallback)
        await session.flush()
        await append_initial_transition(session, fallback)
        failed.fallback_instance_id = fallback.id
        fallbacks.add(1, {"outcome": "created", "node": node.name})
        logger.info(
            "shard fallback created instance_id=%s fallback_instance_id=%s node=%s variant_id=%s",
            failed.id,
            fallback.id,
            node.name,
            variant.id,
        )
