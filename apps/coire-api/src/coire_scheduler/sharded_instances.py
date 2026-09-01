"""Atomic two-Studio admission and durable group lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import uuid
from datetime import UTC, datetime
from pathlib import Path

import anyio
from opentelemetry import metrics, trace
from sqlalchemy import func, select

from coire_api.db import (
    EngineProcessRow,
    EvictionEventRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelVariantRow,
    NodeMemoryLedgerRow,
    NodeRow,
    PlacementCommandRow,
    PlacementDecisionRow,
    RequestLeaseRow,
    ShardCommandRow,
    ShardGroupRow,
    VariantCopyRow,
    session_scope,
)
from coire_api.instance.service import transition
from coire_api.nodes_client import NodeClient
from coire_api.placement.service import node_admission_locks
from coire_api.sharding import link_projection
from coire_core.models import (
    InstanceState,
    LinkState,
    MemoryReservationState,
    PlacementState,
    RdmaState,
    ReservationHolder,
    ShardCapabilityRequest,
    ShardGroupCommand,
    ShardGroupState,
    ShardingMode,
    ShardRank,
)
from coire_core.models.engine import EngineState
from coire_core.models.node import Reachability
from coire_core.settings import get_settings
from coire_scheduler.placement import (
    AdmissionPlan,
    Candidate,
    CapacityRefused,
    NodeCapacity,
    plan_admission,
)

COMMAND_NAMESPACE = uuid.UUID("97f81cc1-853f-478d-961a-30a82e146d67")
tracer = trace.get_tracer("coire.scheduler.sharding")
meter = metrics.get_meter("coire.scheduler.sharding")
group_transitions = meter.create_counter("coire_sharding_group_transitions_total", unit="1")
admission_refusals = meter.create_counter("coire_sharding_admission_refusals_total", unit="1")
logger = logging.getLogger(__name__)


async def _wait(command_id: uuid.UUID) -> dict[str, object]:
    settings = get_settings()
    while True:
        async with session_scope() as session:
            row = await session.get(ShardCommandRow, command_id)
            if row is None:
                raise RuntimeError("shard command disappeared")
            if row.state == "succeeded":
                return dict(row.result or {})
            if row.state == "failed":
                raise RuntimeError(row.failure_detail or "shard command failed")
        await asyncio.sleep(settings.placement_poll_interval_s)


async def _wait_eviction(command_id: uuid.UUID) -> None:
    settings = get_settings()
    while True:
        async with session_scope() as session:
            row = await session.get(PlacementCommandRow, command_id)
            if row is None:
                raise RuntimeError("sharded eviction command disappeared")
            if row.state == "succeeded":
                return
            if row.state == "failed":
                raise RuntimeError(row.failure_detail or "sharded eviction failed")
        await asyncio.sleep(settings.placement_poll_interval_s)


def _command_id(group_id: uuid.UUID, node: str, operation: str) -> uuid.UUID:
    return uuid.uuid5(COMMAND_NAMESPACE, f"{group_id}:{node}:{operation}")


async def _execute_sharded_launch(instance_id: uuid.UUID) -> None:
    settings = get_settings()
    async with session_scope() as session:
        instance = await session.get(ModelInstanceRow, instance_id)
        if instance is None or not instance.policy.startswith("sharded:"):
            raise RuntimeError("not a sharded instance")
        variant = await session.get(ModelVariantRow, instance.variant_id)
        if variant is None or not variant.validated:
            await transition(
                session,
                instance.id,
                InstanceState.FAILED,
                reason="verified variant disappeared",
                failure_code="variant_missing",
            )
            return
        mode = ShardingMode(instance.policy.split(":", 1)[1])
        projection = await link_projection(session, settings)
        if mode is ShardingMode.TENSOR_PARALLEL and projection.rdma_state is not RdmaState.UP:
            admission_refusals.add(1, {"reason": "rdma_probe_required", "mode": mode.value})
            await transition(
                session,
                instance.id,
                InstanceState.FAILED,
                reason=projection.reason or "current successful RDMA evidence is required",
                failure_code="rdma_probe_required",
            )
            return
        if mode is ShardingMode.PIPELINE_PARALLEL and projection.fallback_state is not LinkState.UP:
            admission_refusals.add(1, {"reason": "ring_probe_required", "mode": mode.value})
            await transition(
                session,
                instance.id,
                InstanceState.FAILED,
                reason="current successful ring evidence is required",
                failure_code="ring_probe_required",
            )
            return
        nodes = list(
            (
                await session.execute(
                    select(NodeRow)
                    .join(VariantCopyRow, VariantCopyRow.node_id == NodeRow.id)
                    .where(
                        NodeRow.name.in_(["coire-edge-a", "coire-edge-b"]),
                        NodeRow.reachability == Reachability.HEALTHY,
                        VariantCopyRow.variant_id == variant.id,
                        VariantCopyRow.verified.is_(True),
                    )
                    .order_by(NodeRow.name)
                )
            )
            .scalars()
            .all()
        )
        if [node.name for node in nodes] != ["coire-edge-a", "coire-edge-b"]:
            admission_refusals.add(1, {"reason": "shard_nodes_ineligible", "mode": mode.value})
            await transition(
                session,
                instance.id,
                InstanceState.FAILED,
                reason="both healthy Studios require a verified variant copy",
                failure_code="shard_nodes_ineligible",
            )
            return
        async with NodeClient(settings, timeout=60) as client:
            capability = await client.shard_capability(
                nodes[0].name,
                ShardCapabilityRequest(slug=variant.slug, mode=mode),
            )
        if not capability.supported:
            admission_refusals.add(1, {"reason": "unsupported_architecture", "mode": mode.value})
            await transition(
                session,
                instance.id,
                InstanceState.FAILED,
                reason=(
                    f"architecture {capability.architecture} does not support "
                    f"{mode.value} parallelism"
                ),
                failure_code="unsupported_architecture",
            )
            return
        ledgers = {
            row.node_id: row
            for row in (
                await session.execute(
                    select(NodeMemoryLedgerRow).where(
                        NodeMemoryLedgerRow.node_id.in_([node.id for node in nodes])
                    )
                )
            ).scalars()
        }
        required = max(1, math.ceil(variant.memory_estimate_bytes / 2))
        await transition(session, instance.id, InstanceState.RESERVING, reason="two-node admission")
        eviction_command_ids: list[uuid.UUID] = []
        async with node_admission_locks(session, [node.id for node in nodes]):
            figures: list[str] = []
            plans: dict[uuid.UUID, AdmissionPlan] = {}
            now = datetime.now(UTC)
            active = dict(
                (
                    await session.execute(
                        select(RequestLeaseRow.reservation_id, func.count(RequestLeaseRow.id))
                        .where(
                            RequestLeaseRow.released_at.is_(None),
                            RequestLeaseRow.expires_at > now,
                        )
                        .group_by(RequestLeaseRow.reservation_id)
                    )
                )
                .tuples()
                .all()
            )
            for node in nodes:
                ledger = ledgers.get(node.id)
                node_reservations = list(
                    (
                        await session.execute(
                            select(MemoryReservationRow).where(
                                MemoryReservationRow.node_id == node.id,
                                MemoryReservationRow.state.in_(
                                    [
                                        MemoryReservationState.PENDING,
                                        MemoryReservationState.HELD,
                                        MemoryReservationState.RELEASING,
                                    ]
                                ),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                reserved = sum(row.bytes for row in node_reservations)
                candidates = [
                    Candidate(
                        reservation_id=row.id,
                        holder_id=row.holder_id,
                        bytes=row.bytes,
                        pinned=row.pinned,
                        in_flight=active.get(row.id, 0),
                        last_used_at=row.last_used_at,
                    )
                    for row in node_reservations
                    if row.holder_type is ReservationHolder.MODEL
                    and row.state is MemoryReservationState.HELD
                ]
                try:
                    if ledger is None:
                        raise CapacityRefused([])
                    plans[node.id] = plan_admission(
                        NodeCapacity(budget_bytes=ledger.budget_bytes, reserved_bytes=reserved),
                        required,
                        candidates,
                    )
                except CapacityRefused:
                    figures.append(
                        f"{node.name}: required={required} reserved={reserved} "
                        f"budget={ledger.budget_bytes if ledger else 0}"
                    )
            if figures:
                admission_refusals.add(1, {"reason": "capacity", "mode": mode.value})
                await transition(
                    session,
                    instance.id,
                    InstanceState.FAILED,
                    reason="; ".join(figures),
                    failure_code="sharded_capacity",
                )
                return
            decision = PlacementDecisionRow(
                model_id=instance.model_id,
                variant_id=variant.id,
                policy=instance.policy,
                required_bytes=required * 2,
                state=(
                    PlacementState.EVICTING
                    if any(plan.evictions for plan in plans.values())
                    else PlacementState.RESERVING
                ),
            )
            session.add(decision)
            await session.flush()
            instance.placement_decision_id = decision.id
            all_evictions: list[str] = []
            for node in nodes:
                plan = plans[node.id]
                for rank, reservation_id in enumerate(plan.evictions, start=1):
                    reservation = await session.get(MemoryReservationRow, reservation_id)
                    if reservation is None:
                        raise RuntimeError("planned sharded eviction disappeared")
                    try:
                        holder_uuid = uuid.UUID(reservation.holder_id)
                    except ValueError as exc:
                        raise RuntimeError("model reservation holder is not a UUID") from exc
                    engine = await session.scalar(
                        select(EngineProcessRow).where(
                            EngineProcessRow.node_id == node.id,
                            (
                                (EngineProcessRow.instance_id == holder_uuid)
                                | (EngineProcessRow.model_id == holder_uuid)
                            ),
                            EngineProcessRow.state.in_([EngineState.READY, EngineState.STARTING]),
                        )
                    )
                    if engine is None:
                        raise RuntimeError("held eviction reservation has no running engine")
                    victim = await session.get(ModelInstanceRow, holder_uuid)
                    if victim is not None and victim.state is InstanceState.READY:
                        await transition(
                            session,
                            victim.id,
                            InstanceState.DRAINING,
                            reason="coordinated sharded admission eviction",
                        )
                    reservation.state = MemoryReservationState.RELEASING
                    command_id = uuid.uuid5(
                        COMMAND_NAMESPACE, f"{instance.id}:{node.id}:evict:{reservation.id}"
                    )
                    session.add(
                        PlacementCommandRow(
                            id=command_id,
                            decision_id=decision.id,
                            node_id=node.id,
                            reservation_id=reservation.id,
                            engine_id=engine.id,
                            operation="unload",
                        )
                    )
                    session.add(
                        EvictionEventRow(
                            decision_id=decision.id,
                            node_id=node.id,
                            reservation_id=reservation.id,
                            lru_rank=rank,
                            skipped=[
                                item.model_dump(mode="json")
                                for item in plan.occupants
                                if item.reservation_id not in plan.evictions
                            ],
                            outcome="requested",
                        )
                    )
                    eviction_command_ids.append(command_id)
                    all_evictions.append(str(reservation.id))
            decision.evicted_reservation_ids = all_evictions
            reservations: list[MemoryReservationRow] = []
            for node in nodes:
                reservation = MemoryReservationRow(
                    node_id=node.id,
                    holder_type=ReservationHolder.MODEL,
                    holder_id=str(instance.id),
                    bytes=required,
                    pinned=False,
                    state=MemoryReservationState.PENDING,
                )
                session.add(reservation)
                reservations.append(reservation)
            await session.flush()
            hostfile_path = Path(
                settings.sharding_jaccl_hostfile
                if mode is ShardingMode.TENSOR_PARALLEL
                else settings.sharding_ring_hostfile
            )
            hostfile = await anyio.to_thread.run_sync(hostfile_path.read_bytes)
            digest = hashlib.sha256(hostfile).hexdigest()
            group_id = uuid.uuid5(uuid.NAMESPACE_URL, f"coire:shard-group:{instance.id}")
            command_id = uuid.uuid5(uuid.NAMESPACE_URL, f"coire:shard-command:{instance.id}")
            ranks = [
                ShardRank(
                    rank=rank,
                    node_name=node.name,
                    host=node.data_host or "",
                    port=9600,
                )
                for rank, node in enumerate(nodes)
            ]
            command = ShardGroupCommand(
                command_id=command_id,
                group_id=group_id,
                instance_id=instance.id,
                variant_id=variant.id,
                slug=variant.slug,
                mode=mode,
                ranks=ranks,
                estimate_bytes_per_rank=required,
                hostfile_sha256=digest,
            )
            group = ShardGroupRow(
                id=group_id,
                instance_id=instance.id,
                mode=mode,
                state=ShardGroupState.PREPARING,
                command_id=command_id,
                hostfile_sha256=digest,
            )
            session.add(group)
            await session.flush()
            for rank, (node, reservation) in enumerate(zip(nodes, reservations, strict=True)):
                session.add(
                    InstanceMemberRow(
                        instance_id=instance.id,
                        node_id=node.id,
                        rank=rank,
                        reservation_id=reservation.id,
                        host=node.data_host or "",
                        port=9600 if rank == 0 else None,
                        rank_healthy=False,
                    )
                )
            # Do not expose rank commands to the executor until every coordinated
            # eviction has completed on both ledgers.
    for eviction_command_id in eviction_command_ids:
        await _wait_eviction(eviction_command_id)

    async with session_scope() as session:
        loaded = await session.get(ModelInstanceRow, instance_id)
        if loaded is None:
            raise RuntimeError("sharded instance disappeared after eviction")
        loaded_decision = (
            await session.get(PlacementDecisionRow, loaded.placement_decision_id)
            if loaded.placement_decision_id is not None
            else None
        )
        if loaded_decision is not None:
            loaded_decision.state = PlacementState.RESERVING
        # Rank one records expectation before rank zero starts the distributed launcher.
        for node in reversed(nodes):
            session.add(
                ShardCommandRow(
                    id=_command_id(group_id, node.name, "prepare"),
                    group_id=group_id,
                    node_id=node.id,
                    operation="prepare",
                    payload=command.model_dump(mode="json"),
                )
            )
        await transition(
            session, loaded.id, InstanceState.LAUNCHING, reason="group commands queued"
        )

    for node in reversed(nodes):
        await _wait(_command_id(group_id, node.name, "prepare"))
    async with session_scope() as session:
        loaded_group = await session.get(ShardGroupRow, group_id)
        if loaded_group is None:
            raise RuntimeError("shard group disappeared")
        loaded_group.state = ShardGroupState.STARTING
        await transition(
            session, instance_id, InstanceState.WARMING, reason="distributed first generation"
        )
        for node in nodes:
            session.add(
                ShardCommandRow(
                    id=_command_id(group_id, node.name, "ready"),
                    group_id=group_id,
                    node_id=node.id,
                    operation="ready",
                )
            )
    for node in nodes:
        await _wait(_command_id(group_id, node.name, "ready"))
    async with session_scope() as session:
        ready_group = await session.get(ShardGroupRow, group_id)
        ready_instance = await session.get(ModelInstanceRow, instance_id)
        if ready_group is None or ready_instance is None:
            raise RuntimeError("sharded state disappeared")
        members = list(
            (
                await session.execute(
                    select(InstanceMemberRow).where(InstanceMemberRow.instance_id == instance_id)
                )
            )
            .scalars()
            .all()
        )
        held_reservations: list[MemoryReservationRow] = []
        for member in members:
            if member.reservation_id is not None:
                held = await session.get(MemoryReservationRow, member.reservation_id)
                if held is not None:
                    held_reservations.append(held)
        async with node_admission_locks(session, [member.node_id for member in members]):
            for reservation in held_reservations:
                if reservation is not None:
                    reservation.state = MemoryReservationState.HELD
            for member in members:
                member.rank_healthy = True
                member.last_rank_health_at = datetime.now(UTC)
                recovered_node = await session.get(NodeRow, member.node_id)
                ledger = await session.get(NodeMemoryLedgerRow, member.node_id)
                if (
                    recovered_node is not None
                    and recovered_node.reachability is Reachability.DEGRADED
                ):
                    recovered_node.reachability = Reachability.HEALTHY
                if ledger is not None and ledger.health is Reachability.DEGRADED:
                    ledger.health = Reachability.HEALTHY
                    ledger.health_reason = None
            ready_group.state = ShardGroupState.READY
            if ready_instance.placement_decision_id is not None:
                ready_decision = await session.get(
                    PlacementDecisionRow, ready_instance.placement_decision_id
                )
                if ready_decision is not None:
                    ready_decision.state = PlacementState.READY
            await transition(
                session, ready_instance.id, InstanceState.READY, reason="both ranks ready"
            )
            group_transitions.add(1, {"state": "ready", "mode": mode.value})
            logger.info(
                "shard group ready instance_id=%s group_id=%s mode=%s ranks=2",
                instance_id,
                group_id,
                mode.value,
            )


async def execute_sharded_launch(instance_id: uuid.UUID) -> None:
    with tracer.start_as_current_span("coire.sharding.launch") as span:
        span.set_attribute("instance_id", str(instance_id))
        await _execute_sharded_launch(instance_id)


async def release_failed_reservations(instance_id: uuid.UUID) -> None:
    """Fail any reservations left by a sharded admission that refused before group creation."""
    async with session_scope() as session:
        instance = await session.get(ModelInstanceRow, instance_id)
        if instance is None or instance.state is not InstanceState.FAILED:
            return
        reservations = (
            (
                await session.execute(
                    select(MemoryReservationRow).where(
                        MemoryReservationRow.holder_type == ReservationHolder.MODEL,
                        MemoryReservationRow.holder_id == str(instance_id),
                        MemoryReservationRow.state.in_(
                            [
                                MemoryReservationState.PENDING,
                                MemoryReservationState.HELD,
                                MemoryReservationState.RELEASING,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for reservation in reservations:
            reservation.state = MemoryReservationState.FAILED
            reservation.released_at = datetime.now(UTC)


async def teardown_sharded(instance_id: uuid.UUID, *, failed: bool, reason: str) -> None:
    """Stop both expectations, then release both reservations in one transaction."""
    span = trace.get_current_span()
    span.set_attribute("coire.sharding.instance_id", str(instance_id))
    async with session_scope() as session:
        group = await session.scalar(
            select(ShardGroupRow).where(ShardGroupRow.instance_id == instance_id)
        )
        members = list(
            (
                await session.execute(
                    select(InstanceMemberRow).where(InstanceMemberRow.instance_id == instance_id)
                )
            )
            .scalars()
            .all()
        )
        if group is None:
            return
        group.state = ShardGroupState.STOPPING
        for member in members:
            command_id = _command_id(group.id, str(member.node_id), "stop")
            if await session.get(ShardCommandRow, command_id) is None:
                session.add(
                    ShardCommandRow(
                        id=command_id,
                        group_id=group.id,
                        node_id=member.node_id,
                        operation="stop",
                    )
                )
        ids = [_command_id(group.id, str(member.node_id), "stop") for member in members]
    for command_id in ids:
        try:
            await _wait(command_id)
        except RuntimeError:
            # Continue issuing every stop. The group remains failed and reservations are marked
            # failed rather than falsely free if a node cannot confirm teardown.
            failed = True
    async with session_scope() as session:
        group = await session.scalar(
            select(ShardGroupRow).where(ShardGroupRow.instance_id == instance_id)
        )
        instance = await session.get(ModelInstanceRow, instance_id)
        members = list(
            (
                await session.execute(
                    select(InstanceMemberRow).where(InstanceMemberRow.instance_id == instance_id)
                )
            )
            .scalars()
            .all()
        )
        async with node_admission_locks(session, [member.node_id for member in members]):
            for member in members:
                member.rank_healthy = False
                if member.reservation_id is not None:
                    reservation = await session.get(MemoryReservationRow, member.reservation_id)
                    if reservation is not None:
                        reservation.state = (
                            MemoryReservationState.FAILED
                            if failed
                            else MemoryReservationState.RELEASED
                        )
                        reservation.released_at = datetime.now(UTC)
            if group is not None:
                group.state = ShardGroupState.FAILED if failed else ShardGroupState.STOPPED
                group.state_reason = reason
                group.stopped_at = datetime.now(UTC)
                group_transitions.add(1, {"state": group.state.value, "mode": group.mode.value})
            if instance is not None:
                await transition(
                    session,
                    instance.id,
                    InstanceState.FAILED if failed else InstanceState.STOPPED,
                    reason=reason,
                    failure_code="shard_group_failed" if failed else None,
                )
            logger.info(
                "shard group teardown instance_id=%s group_id=%s failed=%s reason=%s",
                instance_id,
                group.id if group is not None else "missing",
                failed,
                reason,
            )


async def drain_sharded(instance_id: uuid.UUID) -> None:
    settings = get_settings()
    while True:
        async with session_scope() as session:
            instance = await session.get(ModelInstanceRow, instance_id)
            if instance is None:
                return
            members = list(
                (
                    await session.execute(
                        select(InstanceMemberRow).where(
                            InstanceMemberRow.instance_id == instance_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            reservation_ids = [
                member.reservation_id for member in members if member.reservation_id is not None
            ]
            active = await session.scalar(
                select(func.count(RequestLeaseRow.id)).where(
                    RequestLeaseRow.reservation_id.in_(reservation_ids),
                    RequestLeaseRow.released_at.is_(None),
                    RequestLeaseRow.expires_at > datetime.now(UTC),
                )
            )
            expired = (
                instance.drain_deadline is not None and datetime.now(UTC) >= instance.drain_deadline
            )
            if not active or expired:
                if expired:
                    leases = list(
                        (
                            await session.execute(
                                select(RequestLeaseRow).where(
                                    RequestLeaseRow.reservation_id.in_(reservation_ids),
                                    RequestLeaseRow.released_at.is_(None),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for lease in leases:
                        lease.released_at = datetime.now(UTC)
                break
        await asyncio.sleep(settings.placement_poll_interval_s)
    await teardown_sharded(instance_id, failed=False, reason="coordinated drain completed")
