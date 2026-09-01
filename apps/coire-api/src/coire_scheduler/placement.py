"""Deterministic, reservation-authoritative placement policy and DBOS orchestration."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from dbos import DBOS
from opentelemetry import metrics, trace
from sqlalchemy import func, select

from coire_api.db import (
    EngineProcessRow,
    EvictionEventRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelRow,
    ModelVariantRow,
    NodeMemoryLedgerRow,
    NodeRow,
    PlacementCommandRow,
    PlacementDecisionRow,
    RequestLeaseRow,
    VariantCopyRow,
    session_scope,
)
from coire_api.placement.service import ensure_ledgers, node_admission_lock
from coire_core.models.acquisition import VariantState
from coire_core.models.engine import EngineState
from coire_core.models.node import Reachability
from coire_core.models.placement import (
    MemoryReservationState,
    OccupantReason,
    PlacementOccupant,
    PlacementState,
    ReservationHolder,
)
from coire_core.settings import get_settings

COMMAND_NAMESPACE = uuid.UUID("c535ef25-a75d-4b4e-9a9e-e4a91ef80d40")
tracer = trace.get_tracer("coire.scheduler.placement")
meter = metrics.get_meter("coire.scheduler.placement")
admissions = meter.create_counter("coire_placement_admissions_total", unit="1")
evictions = meter.create_counter("coire_placement_evictions_total", unit="1")
refusals = meter.create_counter("coire_placement_refusals_total", unit="1")
reserved_gauge = meter.create_gauge("coire_placement_reserved_bytes", unit="By")
queue_seconds = meter.create_histogram("coire_placement_queue_seconds", unit="s")


@dataclass(frozen=True, slots=True)
class Candidate:
    reservation_id: uuid.UUID
    holder_id: str
    bytes: int
    pinned: bool
    in_flight: int
    last_used_at: datetime


@dataclass(frozen=True, slots=True)
class NodeCapacity:
    budget_bytes: int
    reserved_bytes: int


@dataclass(frozen=True, slots=True)
class AdmissionPlan:
    evictions: list[uuid.UUID]
    occupants: list[PlacementOccupant]


class CapacityRefused(RuntimeError):
    def __init__(self, occupants: list[PlacementOccupant]) -> None:
        super().__init__("no eligible reservations can make enough room")
        self.occupants = occupants


def idle_eligible(
    *,
    last_used_at: datetime,
    ttl_seconds: int | None,
    pinned: bool,
    in_flight: int,
    now: datetime,
) -> bool:
    return (
        ttl_seconds is not None
        and not pinned
        and in_flight == 0
        and last_used_at + timedelta(seconds=ttl_seconds) <= now
    )


def node_admissible(
    *,
    reachability: Reachability,
    last_seen_at: datetime,
    now: datetime,
    freshness_seconds: float,
) -> bool:
    return reachability is Reachability.HEALTHY and last_seen_at >= now - timedelta(
        seconds=freshness_seconds
    )


def _occupant(candidate: Candidate) -> PlacementOccupant:
    reason = (
        OccupantReason.PINNED
        if candidate.pinned
        else OccupantReason.IN_USE
        if candidate.in_flight
        else OccupantReason.ELIGIBLE
    )
    return PlacementOccupant(
        reservation_id=candidate.reservation_id,
        holder_id=candidate.holder_id,
        bytes=candidate.bytes,
        reason=reason,
    )


def plan_admission(
    capacity: NodeCapacity, required_bytes: int, candidates: list[Candidate]
) -> AdmissionPlan:
    """Return the minimal LRU eviction set, or a typed actionable refusal."""
    if required_bytes <= 0:
        raise ValueError("required_bytes must be positive")
    shortage = capacity.reserved_bytes + required_bytes - capacity.budget_bytes
    occupants = [_occupant(item) for item in sorted(candidates, key=lambda item: item.last_used_at)]
    if shortage <= 0:
        return AdmissionPlan([], occupants)
    evictions: list[uuid.UUID] = []
    freed = 0
    for candidate in sorted(candidates, key=lambda item: item.last_used_at):
        if candidate.pinned or candidate.in_flight:
            continue
        evictions.append(candidate.reservation_id)
        freed += candidate.bytes
        if freed >= shortage:
            return AdmissionPlan(evictions, occupants)
    raise CapacityRefused(occupants)


async def _wait_command(command_id: uuid.UUID) -> dict[str, object]:
    settings = get_settings()
    while True:
        async with session_scope() as session:
            row = await session.get(PlacementCommandRow, command_id)
            if row is None:
                raise RuntimeError("placement command disappeared")
            if row.state == "succeeded":
                return dict(row.result or {})
            if row.state == "failed":
                raise RuntimeError(row.failure_detail or "placement command failed")
        await asyncio.sleep(settings.placement_poll_interval_s)


def _command_id(decision_id: uuid.UUID, operation: str, target: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(COMMAND_NAMESPACE, f"{decision_id}:{operation}:{target}")


async def _candidate_nodes(
    session: object, decision: PlacementDecisionRow
) -> tuple[list[tuple[NodeRow, NodeMemoryLedgerRow]], list[str]]:
    # Kept as an explicit typed local import because DBOS imports this module before app startup.
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)
    settings = get_settings()
    rows = list(
        (
            await session.execute(
                select(NodeRow, NodeMemoryLedgerRow)
                .join(NodeMemoryLedgerRow, NodeMemoryLedgerRow.node_id == NodeRow.id)
                .join(
                    VariantCopyRow,
                    (VariantCopyRow.node_id == NodeRow.id)
                    & (VariantCopyRow.variant_id == decision.variant_id),
                )
                .where(VariantCopyRow.verified.is_(True))
            )
        ).all()
    )
    explicit = decision.policy.split(":", 1)[1] if decision.policy != "single:auto" else None
    if explicit is not None:
        rows = [item for item in rows if item[0].name == explicit]
    rows.sort(
        key=lambda item: (
            item[1].thermal_state in {"serious", "critical"},
            (item[1].cpu_percent or 0) >= settings.placement_cpu_saturation_percent,
            item[0].name != "coire-edge-a",
            item[1].cpu_percent or 0,
            item[0].name,
        )
    )
    eligible = [
        (node, ledger)
        for node, ledger in rows
        if node_admissible(
            reachability=node.reachability,
            last_seen_at=node.last_seen_at,
            now=datetime.now(UTC),
            freshness_seconds=settings.placement_health_freshness_s,
        )
    ]
    reasons: list[str] = []
    now = datetime.now(UTC)
    for node, ledger in rows:
        if node.reachability is not Reachability.HEALTHY:
            reasons.append(f"{node.name}: {ledger.health_reason or node.reachability.value}")
        elif not node_admissible(
            reachability=node.reachability,
            last_seen_at=node.last_seen_at,
            now=now,
            freshness_seconds=settings.placement_health_freshness_s,
        ):
            reasons.append(f"{node.name}: health sample is stale")
    return eligible, reasons


async def _run_decision(decision_id: uuid.UUID) -> None:
    started = datetime.now(UTC)
    async with session_scope() as session:
        settings = get_settings()
        await ensure_ledgers(
            session,
            budget_bytes=settings.placement_default_budget_bytes,
            sandbox_bytes=settings.placement_sandbox_bytes,
        )
        decision = await session.get(PlacementDecisionRow, decision_id)
        if decision is None or decision.state is PlacementState.READY:
            return
        model = await session.get(ModelRow, decision.model_id)
        variant = await session.get(ModelVariantRow, decision.variant_id)
        if model is None or variant is None:
            raise RuntimeError("placement references missing registry state")
        nodes, health_reasons = await _candidate_nodes(session, decision)
        if not nodes:
            decision.state = PlacementState.REFUSED
            decision.refusal_code = "no_healthy_verified_copy"
            suffix = f" ({'; '.join(health_reasons)})" if health_reasons else ""
            decision.refusal_detail = "no healthy node with a fresh verified variant copy" + suffix
            refusals.add(1, {"reason": decision.refusal_code})
            return

    last_refusal: CapacityRefused | None = None
    for node, _ in nodes:
        required_bytes = 0
        async with session_scope() as session:
            decision = await session.get(PlacementDecisionRow, decision_id)
            model = await session.get(ModelRow, decision.model_id) if decision else None
            variant = await session.get(ModelVariantRow, decision.variant_id) if decision else None
            ledger = await session.get(NodeMemoryLedgerRow, node.id)
            if decision is None or model is None or variant is None or ledger is None:
                raise RuntimeError("placement state disappeared")
            instance = await session.scalar(
                select(ModelInstanceRow).where(
                    ModelInstanceRow.placement_decision_id == decision.id
                )
            )
            target_holder_id = str(instance.id if instance is not None else model.id)
            async with node_admission_lock(session, node.id):
                reservations = (
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
                candidates = [
                    Candidate(
                        reservation_id=row.id,
                        holder_id=row.holder_id,
                        bytes=row.bytes,
                        pinned=row.pinned,
                        in_flight=active.get(row.id, 0),
                        last_used_at=row.last_used_at,
                    )
                    for row in reservations
                    if row.holder_type is ReservationHolder.MODEL
                    and row.state is MemoryReservationState.HELD
                ]
                try:
                    plan = plan_admission(
                        NodeCapacity(
                            budget_bytes=ledger.budget_bytes,
                            reserved_bytes=sum(item.bytes for item in reservations),
                        ),
                        decision.required_bytes,
                        candidates,
                    )
                except CapacityRefused as exc:
                    last_refusal = exc
                    busy = any(item.reason is OccupantReason.IN_USE for item in exc.occupants)
                    drain_seconds = get_settings().placement_busy_drain_timeout_s
                    if not busy or drain_seconds <= 0:
                        continue
                    decision.state = PlacementState.WAITING_FOR_DRAIN
                    await session.flush()
                    await asyncio.sleep(drain_seconds)
                    refreshed_active = dict(
                        (
                            await session.execute(
                                select(
                                    RequestLeaseRow.reservation_id,
                                    func.count(RequestLeaseRow.id),
                                )
                                .where(
                                    RequestLeaseRow.released_at.is_(None),
                                    RequestLeaseRow.expires_at > datetime.now(UTC),
                                )
                                .group_by(RequestLeaseRow.reservation_id)
                            )
                        )
                        .tuples()
                        .all()
                    )
                    drained = [
                        Candidate(
                            reservation_id=item.reservation_id,
                            holder_id=item.holder_id,
                            bytes=item.bytes,
                            pinned=item.pinned,
                            in_flight=refreshed_active.get(item.reservation_id, 0),
                            last_used_at=item.last_used_at,
                        )
                        for item in candidates
                    ]
                    try:
                        plan = plan_admission(
                            NodeCapacity(
                                budget_bytes=ledger.budget_bytes,
                                reserved_bytes=sum(item.bytes for item in reservations),
                            ),
                            decision.required_bytes,
                            drained,
                        )
                    except CapacityRefused as drained_exc:
                        last_refusal = drained_exc
                        continue
                decision.selected_node_id = node.id
                decision.state = (
                    PlacementState.EVICTING if plan.evictions else PlacementState.RESERVING
                )
                decision.occupants = [item.model_dump(mode="json") for item in plan.occupants]
                target_reservation = await session.scalar(
                    select(MemoryReservationRow).where(
                        MemoryReservationRow.node_id == node.id,
                        MemoryReservationRow.holder_type == ReservationHolder.MODEL,
                        MemoryReservationRow.holder_id == target_holder_id,
                    )
                )
                if target_reservation is None:
                    session.add(
                        MemoryReservationRow(
                            node_id=node.id,
                            holder_type=ReservationHolder.MODEL,
                            holder_id=target_holder_id,
                            bytes=decision.required_bytes,
                            pinned=decision.policy.startswith("pinned:"),
                            state=MemoryReservationState.PENDING,
                        )
                    )
                else:
                    target_reservation.bytes = decision.required_bytes
                    target_reservation.state = MemoryReservationState.PENDING
                    target_reservation.released_at = None
                await session.flush()

        for rank, reservation_id in enumerate(plan.evictions, start=1):
            async with session_scope() as session:
                reservation = await session.get(MemoryReservationRow, reservation_id)
                decision = await session.get(PlacementDecisionRow, decision_id)
                if reservation is None or decision is None:
                    raise RuntimeError("eviction target disappeared")
                engine = await session.scalar(
                    select(EngineProcessRow).where(
                        EngineProcessRow.node_id == node.id,
                        (
                            (EngineProcessRow.instance_id == uuid.UUID(reservation.holder_id))
                            | (EngineProcessRow.model_id == uuid.UUID(reservation.holder_id))
                        ),
                        EngineProcessRow.state.in_([EngineState.READY, EngineState.STARTING]),
                    )
                )
                if engine is None:
                    raise RuntimeError("held model reservation has no running engine")
                reservation.state = MemoryReservationState.RELEASING
                command_id = _command_id(decision_id, "unload", reservation_id)
                if await session.get(PlacementCommandRow, command_id) is None:
                    session.add(
                        PlacementCommandRow(
                            id=command_id,
                            decision_id=decision_id,
                            node_id=node.id,
                            reservation_id=reservation.id,
                            engine_id=engine.id,
                            operation="unload",
                        )
                    )
                    session.add(
                        EvictionEventRow(
                            decision_id=decision_id,
                            node_id=node.id,
                            reservation_id=reservation.id,
                            lru_rank=rank,
                            skipped=[
                                item.model_dump(mode="json")
                                for item in plan.occupants
                                if item.reason is not OccupantReason.ELIGIBLE
                            ],
                            outcome="requested",
                        )
                    )
            await _wait_command(command_id)
            evictions.add(1, {"node": node.name, "reason": "pressure"})

        async with session_scope() as session:
            decision = await session.get(PlacementDecisionRow, decision_id)
            model = await session.get(ModelRow, decision.model_id) if decision else None
            variant = await session.get(ModelVariantRow, decision.variant_id) if decision else None
            if decision is None or model is None or variant is None:
                raise RuntimeError("placement state disappeared before reserve")
            instance = await session.scalar(
                select(ModelInstanceRow).where(
                    ModelInstanceRow.placement_decision_id == decision.id
                )
            )
            target_holder_id = str(instance.id if instance is not None else model.id)
            async with node_admission_lock(session, node.id):
                reservation = await session.scalar(
                    select(MemoryReservationRow).where(
                        MemoryReservationRow.node_id == node.id,
                        MemoryReservationRow.holder_type == ReservationHolder.MODEL,
                        MemoryReservationRow.holder_id == target_holder_id,
                    )
                )
                if reservation is None:
                    reservation = MemoryReservationRow(
                        node_id=node.id,
                        holder_type=ReservationHolder.MODEL,
                        holder_id=target_holder_id,
                        bytes=decision.required_bytes,
                        pinned=decision.policy.startswith("pinned:"),
                        state=MemoryReservationState.PENDING,
                    )
                    session.add(reservation)
                    await session.flush()
                else:
                    reservation.bytes = decision.required_bytes
                    reservation.state = MemoryReservationState.PENDING
                    reservation.released_at = None
                engine = await session.scalar(
                    select(EngineProcessRow).where(
                        EngineProcessRow.node_id == node.id,
                        EngineProcessRow.model_id == model.id,
                    )
                )
                if engine is None:
                    engine = EngineProcessRow(
                        instance_id=instance.id if instance is not None else None,
                        model_id=model.id,
                        node_id=node.id,
                        port=0,
                        state=EngineState.STARTING,
                        estimate_bytes=decision.required_bytes,
                    )
                    session.add(engine)
                    await session.flush()
                command_id = _command_id(decision_id, "load", engine.id)
                if await session.get(PlacementCommandRow, command_id) is None:
                    session.add(
                        PlacementCommandRow(
                            id=command_id,
                            decision_id=decision_id,
                            node_id=node.id,
                            reservation_id=reservation.id,
                            engine_id=engine.id,
                            operation="load",
                            payload={
                                "slug": variant.slug,
                                "estimate_bytes": decision.required_bytes,
                                "chat_template": model.chat_template,
                            },
                        )
                    )
                decision.state = PlacementState.LOADING
        await _wait_command(command_id)
        async with session_scope() as session:
            decision = await session.get(PlacementDecisionRow, decision_id)
            if decision is not None:
                required_bytes = decision.required_bytes
                decision.state = PlacementState.READY
                decision.evicted_reservation_ids = [str(item) for item in plan.evictions]
                decision.updated_at = datetime.now(UTC)
        admissions.add(1, {"node": node.name, "outcome": "ready"})
        reserved_gauge.set(required_bytes, {"node": node.name})
        queue_seconds.record((datetime.now(UTC) - started).total_seconds(), {"outcome": "ready"})
        return

    async with session_scope() as session:
        decision = await session.get(PlacementDecisionRow, decision_id)
        if decision is not None:
            decision.state = PlacementState.REFUSED
            decision.refusal_code = "capacity"
            decision.refusal_detail = "no eligible reservation can make enough room"
            decision.occupants = [
                item.model_dump(mode="json")
                for item in (last_refusal.occupants if last_refusal else [])
            ]
    refusals.add(1, {"reason": "capacity"})


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1.0)
async def execute_placement(decision_id: str) -> None:
    with tracer.start_as_current_span("coire.scheduler.placement") as span:
        span.set_attribute("coire.placement.decision_id", decision_id)
        parsed = uuid.UUID(decision_id)
        try:
            await _run_decision(parsed)
        except Exception as exc:
            span.record_exception(exc)
            async with session_scope() as session:
                decision = await session.get(PlacementDecisionRow, parsed)
                if decision is not None:
                    decision.state = PlacementState.FAILED
                    decision.refusal_code = type(exc).__name__.lower()[:64]
                    decision.refusal_detail = "placement failed; inspect the correlated trace"
                    if decision.selected_node_id is not None:
                        instance = await session.scalar(
                            select(ModelInstanceRow).where(
                                ModelInstanceRow.placement_decision_id == decision.id
                            )
                        )
                        reservation = await session.scalar(
                            select(MemoryReservationRow).where(
                                MemoryReservationRow.node_id == decision.selected_node_id,
                                MemoryReservationRow.holder_type == ReservationHolder.MODEL,
                                MemoryReservationRow.holder_id.in_(
                                    [
                                        str(decision.model_id),
                                        str(instance.id) if instance is not None else "",
                                    ]
                                ),
                                MemoryReservationRow.state == MemoryReservationState.PENDING,
                            )
                        )
                        if reservation is not None:
                            reservation.state = MemoryReservationState.FAILED
            raise


@DBOS.workflow(name="coire.placement.workflow", max_recovery_attempts=100)
async def placement_workflow(decision_id: str) -> None:
    await execute_placement(decision_id)


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1.0)
async def execute_idle_ttl_pass() -> int:
    """Queue confirmed unloads for expired, unpinned, lease-free model reservations."""
    now = datetime.now(UTC)
    queued = 0
    async with session_scope() as session:
        reservations = (
            (
                await session.execute(
                    select(MemoryReservationRow).where(
                        MemoryReservationRow.holder_type == ReservationHolder.MODEL,
                        MemoryReservationRow.state == MemoryReservationState.HELD,
                        MemoryReservationRow.pinned.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
        for reservation in reservations:
            holder_uuid = uuid.UUID(reservation.holder_id)
            instance = await session.get(ModelInstanceRow, holder_uuid)
            model = await session.get(
                ModelRow, instance.model_id if instance is not None else holder_uuid
            )
            if model is None:
                continue
            active = await session.scalar(
                select(func.count(RequestLeaseRow.id)).where(
                    RequestLeaseRow.reservation_id == reservation.id,
                    RequestLeaseRow.released_at.is_(None),
                    RequestLeaseRow.expires_at > now,
                )
            )
            if not idle_eligible(
                last_used_at=reservation.last_used_at,
                ttl_seconds=model.idle_ttl_seconds,
                pinned=reservation.pinned,
                in_flight=active or 0,
                now=now,
            ):
                continue
            engine = await session.scalar(
                select(EngineProcessRow).where(
                    EngineProcessRow.model_id == model.id,
                    *(
                        [EngineProcessRow.instance_id == instance.id]
                        if instance is not None
                        else []
                    ),
                    EngineProcessRow.node_id == reservation.node_id,
                    EngineProcessRow.state == EngineState.READY,
                )
            )
            variant = (
                await session.get(ModelVariantRow, instance.variant_id)
                if instance is not None
                else await session.scalar(
                    select(ModelVariantRow)
                    .where(
                        ModelVariantRow.model_id == model.id,
                        ModelVariantRow.validated.is_(True),
                        ModelVariantRow.state == VariantState.READY,
                    )
                    .order_by(ModelVariantRow.is_default.desc(), ModelVariantRow.created_at)
                    .limit(1)
                )
            )
            if engine is None or variant is None:
                continue
            decision_id = uuid.uuid5(
                COMMAND_NAMESPACE, f"ttl:{reservation.id}:{int(now.timestamp())}"
            )
            session.add(
                PlacementDecisionRow(
                    id=decision_id,
                    model_id=model.id,
                    variant_id=variant.id,
                    policy="idle-ttl",
                    required_bytes=reservation.bytes,
                    state=PlacementState.EVICTING,
                    selected_node_id=reservation.node_id,
                    evicted_reservation_ids=[str(reservation.id)],
                )
            )
            # These rows intentionally do not expose ORM relationships. Flush the parent
            # explicitly so PostgreSQL never sees command/event children before the decision.
            await session.flush()
            session.add(
                PlacementCommandRow(
                    id=_command_id(decision_id, "unload", reservation.id),
                    decision_id=decision_id,
                    node_id=reservation.node_id,
                    reservation_id=reservation.id,
                    engine_id=engine.id,
                    operation="unload",
                )
            )
            session.add(
                EvictionEventRow(
                    decision_id=decision_id,
                    node_id=reservation.node_id,
                    reservation_id=reservation.id,
                    lru_rank=1,
                    skipped=[{"reason": "idle_ttl"}],
                    outcome="requested",
                )
            )
            reservation.state = MemoryReservationState.RELEASING
            queued += 1
            evictions.add(1, {"node": str(reservation.node_id), "reason": "idle_ttl"})
    return queued


@DBOS.workflow(name="coire.placement.idle_ttl", max_recovery_attempts=100)
async def idle_ttl_workflow() -> int:
    return await execute_idle_ttl_pass()
