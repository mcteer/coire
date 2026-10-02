"""Shared memory admission for legacy engines without model instances."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    NodeMemoryLedgerRow,
)
from coire_api.placement.service import node_admission_lock
from coire_core.errors import ChatModelUnavailable
from coire_core.models.instance import InstanceState
from coire_core.models.placement import MemoryReservationState, ReservationHolder


async def ensure_legacy_model_hold(
    session: AsyncSession, node_id: uuid.UUID, model_id: uuid.UUID, estimate_bytes: int
) -> MemoryReservationRow:
    """Reserve a legacy model under the caller's node admission transaction lock."""
    existing = await session.scalar(
        select(MemoryReservationRow).where(
            MemoryReservationRow.node_id == node_id,
            MemoryReservationRow.holder_type == ReservationHolder.MODEL,
            MemoryReservationRow.holder_id == str(model_id),
        )
    )
    if existing is not None and existing.state is MemoryReservationState.HELD:
        if existing.bytes < estimate_bytes:
            raise ChatModelUnavailable()
        return existing
    ledger = await session.get(NodeMemoryLedgerRow, node_id, populate_existing=True)
    if ledger is None or estimate_bytes <= 0:
        raise ChatModelUnavailable()
    reservations = (
        await session.scalars(
            select(MemoryReservationRow).where(
                MemoryReservationRow.node_id == node_id,
                MemoryReservationRow.state.in_(
                    (
                        MemoryReservationState.PENDING,
                        MemoryReservationState.HELD,
                        MemoryReservationState.RELEASING,
                    )
                ),
            )
        )
    ).all()
    if any(row.holder_type is ReservationHolder.IMAGE for row in reservations):
        raise ChatModelUnavailable()
    live_image = await session.scalar(
        select(ModelInstanceRow.id)
        .join(InstanceMemberRow, InstanceMemberRow.instance_id == ModelInstanceRow.id)
        .where(
            InstanceMemberRow.node_id == node_id,
            ModelInstanceRow.policy.like("image:%"),
            ModelInstanceRow.state.in_(
                (
                    InstanceState.LAUNCHING,
                    InstanceState.WARMING,
                    InstanceState.READY,
                    InstanceState.DRAINING,
                )
            ),
        )
        .limit(1)
    )
    if live_image is not None:
        raise ChatModelUnavailable()
    occupied = sum(row.bytes for row in reservations if row is not existing)
    if occupied + estimate_bytes > ledger.budget_bytes:
        raise ChatModelUnavailable()
    if existing is None:
        existing = MemoryReservationRow(
            node_id=node_id,
            holder_type=ReservationHolder.MODEL,
            holder_id=str(model_id),
            bytes=estimate_bytes,
            state=MemoryReservationState.HELD,
        )
        session.add(existing)
    else:
        existing.bytes = estimate_bytes
        existing.state = MemoryReservationState.HELD
        existing.released_at = None
    await session.flush()
    return existing


async def ensure_legacy_model_hold_locked(
    session: AsyncSession, node_id: uuid.UUID, model_id: uuid.UUID, estimate_bytes: int
) -> MemoryReservationRow:
    async with node_admission_lock(session, node_id):
        return await ensure_legacy_model_hold(session, node_id, model_id, estimate_bytes)
