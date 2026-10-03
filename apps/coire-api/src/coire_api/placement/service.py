"""Projection and mutation helpers for the authoritative memory ledger."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from opentelemetry import metrics
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import (
    MemoryReservationRow,
    ModelInstanceRow,
    NodeMemoryLedgerRow,
    NodeRow,
    RequestLeaseRow,
)
from coire_core.models.audit import AuditAction
from coire_core.models.instance import InstanceState
from coire_core.models.placement import (
    LedgerUpdate,
    MemoryLedger,
    MemoryReservation,
    MemoryReservationState,
    PinUpdate,
    ReservationHolder,
)


class LedgerNotFoundError(LookupError):
    pass


meter = metrics.get_meter("coire.api.placement")
ledger_drift = meter.create_gauge(
    "coire_placement_ledger_drift_ratio",
    description="Measured model and image residency minus their reservations, divided by those reservations.",
)
image_residency_unavailable = meter.create_gauge(
    "coire_image_residency_measurement_unavailable",
    description="One means an image reservation exists but a live model or image footprint cannot be measured.",
)


def drift_ratio(*, reserved_bytes: int, measured_bytes: int | None) -> float | None:
    if measured_bytes is None:
        return None
    if reserved_bytes == 0:
        return 1.0 if measured_bytes > 0 else None
    return (measured_bytes - reserved_bytes) / reserved_bytes


def resident_reservation_bytes(reservations: Sequence[MemoryReservationRow]) -> int:
    """Match the model and image processes included in measured residency."""
    return sum(
        row.bytes
        for row in reservations
        if row.holder_type in (ReservationHolder.MODEL, ReservationHolder.IMAGE)
    )


def effective_occupied_bytes(
    reservations: Sequence[MemoryReservationRow], measured_resident_bytes: int | None
) -> int | None:
    """Count physical overage once; an unmeasured held image process blocks admission."""
    occupied = sum(row.bytes for row in reservations)
    if measured_resident_bytes is None:
        if any(row.holder_type is ReservationHolder.IMAGE for row in reservations):
            return None
        return occupied
    return occupied + max(0, measured_resident_bytes - resident_reservation_bytes(reservations))


@asynccontextmanager
async def node_admission_lock(session: AsyncSession, node_id: uuid.UUID) -> AsyncIterator[None]:
    """Serialize admissions for one node for the lifetime of the transaction."""
    await lock_nodes_for_admission(session, [node_id])
    yield


async def lock_nodes_for_admission(session: AsyncSession, node_ids: list[uuid.UUID]) -> None:
    """Take transaction-scoped node locks before reading competing placement state."""
    keys = [
        int.from_bytes(item.bytes[:8], "big", signed=False) & ((1 << 63) - 1) for item in node_ids
    ]
    if len(set(node_ids)) != len(node_ids) or len(set(keys)) != len(keys):
        raise ValueError("admission group contains duplicate node lock keys")
    for key in sorted(keys):
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


@asynccontextmanager
async def node_admission_locks(
    session: AsyncSession, node_ids: list[uuid.UUID]
) -> AsyncIterator[None]:
    """Lock a group in stable order so competing sharded admissions cannot split or deadlock."""
    await lock_nodes_for_admission(session, node_ids)
    yield


async def ensure_ledgers(
    session: AsyncSession, *, budget_bytes: int, sandbox_bytes: int, failover_bytes: int = 0
) -> None:
    """Create ledger and standing agent/failover reservations for declared nodes."""
    nodes = (await session.execute(select(NodeRow))).scalars().all()
    for node in nodes:
        ledger = await session.get(NodeMemoryLedgerRow, node.id)
        if ledger is None:
            session.add(
                NodeMemoryLedgerRow(
                    node_id=node.id,
                    budget_bytes=budget_bytes,
                    sandbox_bytes=sandbox_bytes,
                    health=node.reachability,
                )
            )
        sandbox = await session.scalar(
            select(MemoryReservationRow).where(
                MemoryReservationRow.node_id == node.id,
                MemoryReservationRow.holder_type == ReservationHolder.SANDBOX,
                MemoryReservationRow.holder_id == "agent-sandbox",
            )
        )
        if sandbox is None and sandbox_bytes:
            session.add(
                MemoryReservationRow(
                    node_id=node.id,
                    holder_type=ReservationHolder.SANDBOX,
                    holder_id="agent-sandbox",
                    bytes=sandbox_bytes,
                    pinned=True,
                    state=MemoryReservationState.HELD,
                )
            )
        failover = await session.scalar(
            select(MemoryReservationRow).where(
                MemoryReservationRow.node_id == node.id,
                MemoryReservationRow.holder_type == ReservationHolder.SANDBOX,
                MemoryReservationRow.holder_id == "failover-frontend",
            )
        )
        if failover is None and failover_bytes:
            session.add(
                MemoryReservationRow(
                    node_id=node.id,
                    holder_type=ReservationHolder.SANDBOX,
                    holder_id="failover-frontend",
                    bytes=failover_bytes,
                    pinned=True,
                    state=MemoryReservationState.HELD,
                )
            )
    await session.flush()


async def _active_leases(session: AsyncSession) -> dict[uuid.UUID, int]:
    now = datetime.now(UTC)
    rows = await session.execute(
        select(RequestLeaseRow.reservation_id, func.count(RequestLeaseRow.id))
        .where(RequestLeaseRow.released_at.is_(None), RequestLeaseRow.expires_at > now)
        .group_by(RequestLeaseRow.reservation_id)
    )
    active: dict[uuid.UUID, int] = {}
    for reservation_id, count in rows.tuples().all():
        active[reservation_id] = count
    return active


async def project_ledgers(session: AsyncSession) -> list[MemoryLedger]:
    leases = await _active_leases(session)
    nodes = {
        row.id: row
        for row in (await session.execute(select(NodeRow).order_by(NodeRow.name))).scalars()
    }
    ledgers = (await session.execute(select(NodeMemoryLedgerRow))).scalars().all()
    result: list[MemoryLedger] = []
    for ledger in ledgers:
        reservations = (
            (
                await session.execute(
                    select(MemoryReservationRow)
                    .where(
                        MemoryReservationRow.node_id == ledger.node_id,
                        MemoryReservationRow.state.in_(
                            [
                                MemoryReservationState.PENDING,
                                MemoryReservationState.HELD,
                                MemoryReservationState.RELEASING,
                            ]
                        ),
                    )
                    .order_by(MemoryReservationRow.created_at)
                )
            )
            .scalars()
            .all()
        )
        reserved = sum(row.bytes for row in reservations)
        measured = ledger.measured_resident_bytes
        resident_reserved = resident_reservation_bytes(reservations)
        drift = drift_ratio(reserved_bytes=resident_reserved, measured_bytes=measured)
        node = nodes[ledger.node_id]
        ledger_drift.set(drift if drift is not None else 0.0, {"node": node.name})
        result.append(
            MemoryLedger(
                node_id=ledger.node_id,
                node_name=node.name,
                budget_bytes=ledger.budget_bytes,
                sandbox_bytes=ledger.sandbox_bytes,
                reserved_bytes=reserved,
                free_bytes=ledger.budget_bytes - reserved,
                measured_resident_bytes=measured,
                drift_ratio=drift,
                health=ledger.health,
                health_reason=ledger.health_reason,
                health_sampled_at=ledger.health_sampled_at,
                reservations=[
                    MemoryReservation(
                        id=row.id,
                        node_id=row.node_id,
                        holder_type=row.holder_type,
                        holder_id=row.holder_id,
                        bytes=row.bytes,
                        pinned=row.pinned,
                        state=row.state,
                        last_used_at=row.last_used_at,
                        created_at=row.created_at,
                        released_at=row.released_at,
                        in_flight=leases.get(row.id, 0),
                    )
                    for row in reservations
                ],
                updated_at=ledger.updated_at,
            )
        )
    return result


async def update_ledger(
    session: AsyncSession,
    node_id: uuid.UUID,
    update: LedgerUpdate,
    *,
    actor: str,
) -> MemoryLedger:
    ledger = await session.get(NodeMemoryLedgerRow, node_id)
    if ledger is None:
        raise LedgerNotFoundError
    if update.budget_bytes is not None:
        ledger.budget_bytes = update.budget_bytes
    if update.sandbox_bytes is not None:
        ledger.sandbox_bytes = update.sandbox_bytes
        reservation = await session.scalar(
            select(MemoryReservationRow).where(
                MemoryReservationRow.node_id == node_id,
                MemoryReservationRow.holder_type == ReservationHolder.SANDBOX,
            )
        )
        if reservation is not None:
            reservation.bytes = update.sandbox_bytes
            reservation.state = (
                MemoryReservationState.HELD
                if update.sandbox_bytes
                else MemoryReservationState.RELEASED
            )
            reservation.released_at = None if update.sandbox_bytes else datetime.now(UTC)
        elif update.sandbox_bytes:
            session.add(
                MemoryReservationRow(
                    node_id=node_id,
                    holder_type=ReservationHolder.SANDBOX,
                    holder_id="agent-sandbox",
                    bytes=update.sandbox_bytes,
                    pinned=True,
                    state=MemoryReservationState.HELD,
                )
            )
    ledger.updated_at = datetime.now(UTC)
    await write_audit(
        session,
        actor=actor,
        action=AuditAction.LEDGER_UPDATE,
        target_type="node_memory_ledger",
        target_id=str(node_id),
        detail=update.model_dump(exclude_none=True),
    )
    await session.flush()
    return next(item for item in await project_ledgers(session) if item.node_id == node_id)


async def set_pin(
    session: AsyncSession,
    reservation_id: uuid.UUID,
    update: PinUpdate,
    *,
    actor: str,
) -> MemoryReservationRow:
    row = await session.get(MemoryReservationRow, reservation_id)
    if row is None or row.holder_type not in {ReservationHolder.MODEL, ReservationHolder.IMAGE}:
        raise LedgerNotFoundError
    image_worker = row.holder_type is ReservationHolder.IMAGE
    if image_worker:
        async with node_admission_lock(session, row.node_id):
            row = await session.get(
                MemoryReservationRow, reservation_id, populate_existing=True, with_for_update=True
            )
            if row is None or row.state is not MemoryReservationState.HELD:
                raise LedgerNotFoundError
            try:
                instance_id = uuid.UUID(row.holder_id)
            except ValueError as exc:
                raise LedgerNotFoundError from exc
            instance = await session.get(ModelInstanceRow, instance_id)
            if instance is None or instance.state is InstanceState.DRAINING:
                raise LedgerNotFoundError
            row.pinned = update.pinned
        action = "image.worker.pin" if update.pinned else "image.worker.unpin"
    else:
        row.pinned = update.pinned
        action = AuditAction.MODEL_PIN if update.pinned else AuditAction.MODEL_UNPIN
    await write_audit(
        session,
        actor=actor,
        action=action,
        target_type="memory_reservation",
        target_id=str(reservation_id),
        detail={
            "pinned": update.pinned,
            "instance_id" if image_worker else "model_id": row.holder_id,
        },
    )
    return row


async def acquire_lease(
    session: AsyncSession,
    reservation_id: uuid.UUID,
    request_id: str,
    *,
    ttl_seconds: float,
) -> RequestLeaseRow:
    reservation = await session.get(MemoryReservationRow, reservation_id)
    if reservation is None:
        raise LedgerNotFoundError
    # Share the transaction-scoped node lock with image dispatch and eviction.
    # No lease may be inserted from a pre-lock (possibly stale) reservation.
    await lock_nodes_for_admission(session, [reservation.node_id])
    reservation = await session.get(
        MemoryReservationRow, reservation_id, populate_existing=True, with_for_update=True
    )
    if reservation is None or reservation.state is not MemoryReservationState.HELD:
        raise LedgerNotFoundError
    now = datetime.now(UTC)
    row = RequestLeaseRow(
        reservation_id=reservation_id,
        request_id=request_id,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    session.add(row)
    reservation.last_used_at = now
    await session.flush()
    return row


async def release_lease(session: AsyncSession, lease_id: uuid.UUID) -> None:
    row = await session.get(RequestLeaseRow, lease_id)
    if row is not None and row.released_at is None:
        row.released_at = datetime.now(UTC)


async def refresh_lease(session: AsyncSession, lease_id: uuid.UUID, *, ttl_seconds: float) -> bool:
    row = await session.get(RequestLeaseRow, lease_id)
    now = datetime.now(UTC)
    if row is None or row.released_at is not None or row.expires_at <= now or ttl_seconds <= 0:
        return False
    row.expires_at = now + timedelta(seconds=ttl_seconds)
    return True
