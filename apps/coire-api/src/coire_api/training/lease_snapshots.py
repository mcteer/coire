"""Bounded, node-scoped lease observations for the native training stop lane."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    EngineProcessRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    NodeRow,
    RequestLeaseRow,
)
from coire_api.placement.service import lock_nodes_for_admission
from coire_api.training.telemetry import observed
from coire_core.errors import TrainingConflict, TrainingNotFound
from coire_core.models.engine import TERMINAL_ENGINE_STATES
from coire_core.models.instance import TERMINAL_INSTANCE_STATES
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.training_node import NodeTrainingLeaseSnapshot

logger = logging.getLogger(__name__)
_COUNTED = (
    MemoryReservationState.PENDING,
    MemoryReservationState.HELD,
    MemoryReservationState.RELEASING,
)
_MAX_IDENTITIES = 256


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


@observed("coire.api.training.lease_snapshot")
async def node_lease_snapshot(session: AsyncSession, node_name: str) -> NodeTrainingLeaseSnapshot:
    """Read all node leases; unknown ownership contributes a conservative UUID key.

    The caller commits before returning the snapshot. New leases and accelerator
    ownership writers share this admission lock. Zeroes are explicit observations,
    never defaults for identities missing from this bounded inventory.
    """
    node = await session.scalar(select(NodeRow).where(NodeRow.name == node_name))
    if node is None or node.role != "studio" or node_name not in {"coire-edge-a", "coire-edge-b"}:
        raise TrainingNotFound()
    await lock_nodes_for_admission(session, [node.id])
    sampled_at: datetime = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    counts: dict[uuid.UUID, int] = {}

    def add(identity: uuid.UUID, count: int = 0) -> None:
        counts[identity] = counts.get(identity, 0) + count
        if len(counts) > _MAX_IDENTITIES:
            raise TrainingConflict("Node lease inventory exceeds its identity bound")

    members = (
        await session.execute(
            select(InstanceMemberRow.reservation_id, ModelInstanceRow.id, ModelInstanceRow.state)
            .join(ModelInstanceRow, ModelInstanceRow.id == InstanceMemberRow.instance_id)
            .where(
                InstanceMemberRow.node_id == node.id,
                or_(
                    ModelInstanceRow.state.not_in(TERMINAL_INSTANCE_STATES),
                    select(MemoryReservationRow.id)
                    .where(
                        MemoryReservationRow.id == InstanceMemberRow.reservation_id,
                        MemoryReservationRow.holder_type == ReservationHolder.MODEL,
                        MemoryReservationRow.state.in_(_COUNTED),
                    )
                    .exists(),
                    select(RequestLeaseRow.id)
                    .where(
                        RequestLeaseRow.reservation_id == InstanceMemberRow.reservation_id,
                        RequestLeaseRow.released_at.is_(None),
                        RequestLeaseRow.expires_at > sampled_at,
                    )
                    .exists(),
                ),
            )
            .limit(_MAX_IDENTITIES + 1)
        )
    ).all()
    if len(members) > _MAX_IDENTITIES:
        raise TrainingConflict("Node resident inventory exceeds its identity bound")
    member_owners: dict[uuid.UUID, uuid.UUID] = {}
    for reservation_id, instance_id, state in members:
        if state not in TERMINAL_INSTANCE_STATES:
            add(instance_id)
        if reservation_id is not None:
            prior = member_owners.setdefault(reservation_id, instance_id)
            if prior != instance_id:
                raise TrainingConflict("Node lease reservation has ambiguous instance ownership")
    engines = (
        await session.execute(
            select(EngineProcessRow.id, EngineProcessRow.instance_id, EngineProcessRow.model_id)
            .where(
                EngineProcessRow.node_id == node.id,
                EngineProcessRow.state.not_in(TERMINAL_ENGINE_STATES),
            )
            .limit(_MAX_IDENTITIES + 1)
        )
    ).all()
    if len(engines) > _MAX_IDENTITIES:
        raise TrainingConflict("Node engine inventory exceeds its identity bound")
    unowned_engines: set[uuid.UUID] = set()
    for engine_id, instance_id, model_id in engines:
        if instance_id is not None:
            add(instance_id)
        else:
            # An unowned live engine is never evidence of an isolated idle accelerator.
            unowned_engines.add(model_id or engine_id)
    leases = (
        await session.execute(
            select(
                MemoryReservationRow.id,
                MemoryReservationRow.holder_type,
                MemoryReservationRow.holder_id,
                MemoryReservationRow.state,
                func.count(RequestLeaseRow.id),
            )
            .outerjoin(
                RequestLeaseRow,
                and_(
                    RequestLeaseRow.reservation_id == MemoryReservationRow.id,
                    RequestLeaseRow.released_at.is_(None),
                    RequestLeaseRow.expires_at > sampled_at,
                ),
            )
            .where(
                MemoryReservationRow.node_id == node.id,
                or_(
                    and_(
                        MemoryReservationRow.holder_type == ReservationHolder.MODEL,
                        MemoryReservationRow.state.in_(_COUNTED),
                    ),
                    RequestLeaseRow.id.is_not(None),
                ),
            )
            .group_by(MemoryReservationRow.id)
            .limit(_MAX_IDENTITIES + 1)
        )
    ).all()
    if len(leases) > _MAX_IDENTITIES:
        raise TrainingConflict("Node lease holder inventory exceeds its identity bound")
    holder_ids = {
        identity
        for _, kind, holder, _, _ in leases
        if kind is ReservationHolder.MODEL and (identity := _uuid(holder)) is not None
    }
    known_instances = set(
        await session.scalars(
            select(ModelInstanceRow.id).where(ModelInstanceRow.id.in_(holder_ids))
        )
    )
    for reservation_id, kind, holder, state, active in leases:
        holder_id = _uuid(holder) if kind is ReservationHolder.MODEL else None
        owner = member_owners.get(reservation_id)
        if owner is not None and holder_id != owner:
            raise TrainingConflict("Node lease holder differs from its bound instance")
        if owner is None and holder_id in known_instances:
            owner = holder_id
        if owner is not None:
            add(owner, int(active))
        else:
            # Legacy model IDs and unknown/non-model lease owners retain explicit
            # UUID markers. Native isolated probes sum every mapping entry.
            identity = holder_id or reservation_id
            conservative = max(
                int(active), int(kind is ReservationHolder.MODEL and state in _COUNTED)
            )
            add(identity, conservative)
    for identity in unowned_engines:
        counts[identity] = max(counts.get(identity, 0), 1)
        if len(counts) > _MAX_IDENTITIES:
            raise TrainingConflict("Node lease inventory exceeds its identity bound")
    expires_at = sampled_at + timedelta(seconds=5)
    publication_time: datetime = (
        await session.execute(select(func.clock_timestamp()))
    ).scalar_one()
    if publication_time >= expires_at:
        raise TrainingConflict("Node lease observation expired before publication")
    logger.info(
        "training node lease snapshot",
        extra={
            "node": node_name,
            "instance_count": len(counts),
            "active_lease_count": sum(counts.values()),
        },
    )
    return NodeTrainingLeaseSnapshot(
        node=node_name,
        sampled_at=sampled_at,
        expires_at=expires_at,
        active_leases=counts,
    )
