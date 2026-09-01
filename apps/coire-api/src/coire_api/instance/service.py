"""Atomic instance transitions and typed projections."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import InstanceMemberRow, InstanceTransitionRow, ModelInstanceRow, NodeRow
from coire_core.models.instance import (
    InstanceMember,
    InstanceState,
    InstanceTransition,
    ModelInstance,
)
from coire_core.models.node import Reachability

ALLOWED_TRANSITIONS: dict[InstanceState, frozenset[InstanceState]] = {
    InstanceState.REQUESTED: frozenset({InstanceState.RESERVING, InstanceState.FAILED}),
    InstanceState.RESERVING: frozenset({InstanceState.LAUNCHING, InstanceState.FAILED}),
    InstanceState.LAUNCHING: frozenset({InstanceState.WARMING, InstanceState.FAILED}),
    InstanceState.WARMING: frozenset({InstanceState.READY, InstanceState.FAILED}),
    InstanceState.READY: frozenset({InstanceState.DRAINING, InstanceState.FAILED}),
    InstanceState.DRAINING: frozenset({InstanceState.STOPPED, InstanceState.FAILED}),
    InstanceState.STOPPED: frozenset(),
    InstanceState.FAILED: frozenset(),
}


class InvalidInstanceTransition(ValueError):
    pass


async def append_initial_transition(session: AsyncSession, row: ModelInstanceRow) -> None:
    session.add(
        InstanceTransitionRow(
            instance_id=row.id,
            sequence=1,
            previous_state=None,
            state=row.state,
            reason="instance requested",
        )
    )
    await session.flush()


async def transition(
    session: AsyncSession,
    instance_id: uuid.UUID,
    target: InstanceState,
    *,
    reason: str | None = None,
    failure_code: str | None = None,
) -> ModelInstanceRow:
    row = await session.scalar(
        select(ModelInstanceRow).where(ModelInstanceRow.id == instance_id).with_for_update()
    )
    if row is None:
        raise LookupError("instance not found")
    if row.state is target:
        return row
    if target not in ALLOWED_TRANSITIONS[row.state]:
        raise InvalidInstanceTransition(f"cannot transition {row.state.value} to {target.value}")
    sequence = await session.scalar(
        select(func.coalesce(func.max(InstanceTransitionRow.sequence), 0)).where(
            InstanceTransitionRow.instance_id == instance_id
        )
    )
    now = datetime.now(UTC)
    previous = row.state
    row.state = target
    row.transitioned_at = now
    row.updated_at = now
    if target is InstanceState.FAILED:
        row.failure_code = failure_code or "instance_failed"
        row.failure_detail = reason or "instance lifecycle failed"
    session.add(
        InstanceTransitionRow(
            instance_id=instance_id,
            sequence=int(sequence or 0) + 1,
            previous_state=previous,
            state=target,
            reason=reason,
            at=now,
        )
    )
    await write_audit(
        session,
        actor="coire-scheduler",
        action="instance.transition",
        target_type="model_instance",
        target_id=str(instance_id),
        detail={"previous_state": previous.value, "state": target.value, "reason": reason},
    )
    await session.flush()
    return row


async def project_instance(session: AsyncSession, row: ModelInstanceRow) -> ModelInstance:
    member_rows = list(
        (
            await session.execute(
                select(InstanceMemberRow)
                .where(InstanceMemberRow.instance_id == row.id)
                .order_by(InstanceMemberRow.rank)
            )
        )
        .scalars()
        .all()
    )
    node_rows = {
        node.id: node
        for node in (
            await session.execute(
                select(NodeRow).where(NodeRow.id.in_([item.node_id for item in member_rows]))
            )
        )
        .scalars()
        .all()
    }
    effective = row.state
    if row.state is InstanceState.READY and any(
        node_rows.get(item.node_id) is None
        or node_rows[item.node_id].reachability is not Reachability.HEALTHY
        for item in member_rows
    ):
        effective = InstanceState.FAILED
    return ModelInstance(
        id=row.id,
        model_id=row.model_id,
        variant_id=row.variant_id,
        placement_decision_id=row.placement_decision_id,
        policy=row.policy,
        state=row.state,
        effective_state=effective,
        failure_code=row.failure_code,
        failure_detail=row.failure_detail,
        in_flight=row.in_flight,
        members=[
            InstanceMember(
                node_id=item.node_id,
                node_name=node_rows[item.node_id].name,
                rank=item.rank,
                engine_id=item.engine_id,
                reservation_id=item.reservation_id,
                host=item.host,
                port=item.port,
            )
            for item in member_rows
            if item.node_id in node_rows
        ],
        created_at=row.created_at,
        updated_at=row.updated_at,
        transitioned_at=row.transitioned_at,
        drain_deadline=row.drain_deadline,
    )


def project_transition(row: InstanceTransitionRow) -> InstanceTransition:
    return InstanceTransition.model_validate(row, from_attributes=True)
