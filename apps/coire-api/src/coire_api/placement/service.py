"""Projection and mutation helpers for the authoritative memory ledger."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from opentelemetry import metrics
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import (
    MemoryReservationRow,
    NodeMemoryLedgerRow,
    NodeRow,
    RequestLeaseRow,
)
from coire_core.models.audit import AuditAction
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
    unit="1",
    description="Measured model residency minus reservations, divided by reservations.",
)


def drift_ratio(*, reserved_bytes: int, measured_bytes: int | None) -> float | None:
    if measured_bytes is None or reserved_bytes == 0:
        return None
    return (measured_bytes - reserved_bytes) / reserved_bytes


@asynccontextmanager
async def node_admission_lock(session: AsyncSession, node_id: uuid.UUID) -> AsyncIterator[None]:
    """Serialize admissions for one node for the lifetime of the transaction."""
    key = int.from_bytes(node_id.bytes[:8], "big", signed=False) & ((1 << 63) - 1)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    yield


async def ensure_ledgers(session: AsyncSession, *, budget_bytes: int, sandbox_bytes: int) -> None:
    """Create ledger and standing sandbox rows for newly declared nodes."""
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
        drift = drift_ratio(reserved_bytes=reserved, measured_bytes=measured)
        node = nodes[ledger.node_id]
        if drift is not None:
            ledger_drift.set(drift, {"node": node.name})
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
    if row is None or row.holder_type is not ReservationHolder.MODEL:
        raise LedgerNotFoundError
    row.pinned = update.pinned
    await write_audit(
        session,
        actor=actor,
        action=AuditAction.MODEL_PIN if update.pinned else AuditAction.MODEL_UNPIN,
        target_type="memory_reservation",
        target_id=str(reservation_id),
        detail={"pinned": update.pinned, "model_id": row.holder_id},
    )
    return row


async def acquire_lease(
    session: AsyncSession,
    reservation_id: uuid.UUID,
    request_id: str,
    *,
    ttl_seconds: float,
) -> RequestLeaseRow:
    now = datetime.now(UTC)
    row = RequestLeaseRow(
        reservation_id=reservation_id,
        request_id=request_id,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    session.add(row)
    reservation = await session.get(MemoryReservationRow, reservation_id)
    if reservation is None or reservation.state is not MemoryReservationState.HELD:
        raise LedgerNotFoundError
    reservation.last_used_at = now
    await session.flush()
    return row


async def release_lease(session: AsyncSession, lease_id: uuid.UUID) -> None:
    row = await session.get(RequestLeaseRow, lease_id)
    if row is not None and row.released_at is None:
        row.released_at = datetime.now(UTC)


async def refresh_lease(session: AsyncSession, lease_id: uuid.UUID, *, ttl_seconds: float) -> bool:
    row = await session.get(RequestLeaseRow, lease_id)
    if row is None or row.released_at is not None:
        return False
    row.expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    return True
