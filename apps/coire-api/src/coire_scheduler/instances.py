"""DBOS-backed durable model-instance lifecycle."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from dbos import DBOS
from opentelemetry import metrics, trace
from sqlalchemy import func, select

from coire_api.db import (
    EngineProcessRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelVariantRow,
    NodeRow,
    PlacementCommandRow,
    PlacementDecisionRow,
    RequestLeaseRow,
    session_scope,
)
from coire_api.instance.service import transition
from coire_core.models.engine import EngineState
from coire_core.models.instance import InstanceState
from coire_core.models.placement import (
    MemoryReservationState,
    PlacementState,
    ReservationHolder,
)
from coire_core.settings import get_settings

tracer = trace.get_tracer("coire.scheduler.instances")
meter = metrics.get_meter("coire.scheduler.instances")
transitions = meter.create_counter("coire_instance_transitions_total", unit="1")
failures = meter.create_counter("coire_instance_failures_total", unit="1")
logger = logging.getLogger(__name__)


async def _advance(instance_id: uuid.UUID, state: InstanceState, reason: str) -> None:
    async with session_scope() as session:
        row = await session.get(ModelInstanceRow, instance_id)
        if row is not None and row.state is not state:
            await transition(session, instance_id, state, reason=reason)
            transitions.add(1, {"state": state.value})
            logger.info(
                "instance transition instance_id=%s state=%s reason=%s",
                instance_id,
                state.value,
                reason,
            )


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1.0)
async def execute_instance_launch(instance_id_text: str) -> None:
    instance_id = uuid.UUID(instance_id_text)
    settings = get_settings()
    with tracer.start_as_current_span("coire.scheduler.instance.launch") as span:
        span.set_attribute("instance_id", instance_id_text)
        try:
            async with session_scope() as session:
                candidate = await session.get(ModelInstanceRow, instance_id)
                sharded = candidate is not None and candidate.policy.startswith("sharded:")
            if sharded:
                from coire_scheduler.sharded_instances import (
                    execute_sharded_launch,
                    teardown_sharded,
                )

                try:
                    await execute_sharded_launch(instance_id)
                except Exception:
                    await teardown_sharded(instance_id, failed=True, reason="sharded launch failed")
                    raise
                return
            async with session_scope() as session:
                instance = await session.get(ModelInstanceRow, instance_id)
                if instance is None or instance.state in {
                    InstanceState.READY,
                    InstanceState.DRAINING,
                    InstanceState.STOPPED,
                }:
                    return
                variant = await session.get(ModelVariantRow, instance.variant_id)
                if variant is None or not variant.validated:
                    await transition(
                        session,
                        instance_id,
                        InstanceState.FAILED,
                        reason="validated variant disappeared",
                        failure_code="variant_missing",
                    )
                    return
                if instance.state is InstanceState.REQUESTED:
                    await transition(
                        session, instance_id, InstanceState.RESERVING, reason="placement requested"
                    )
                decision = (
                    await session.get(PlacementDecisionRow, instance.placement_decision_id)
                    if instance.placement_decision_id
                    else None
                )
                if decision is None:
                    decision = PlacementDecisionRow(
                        model_id=instance.model_id,
                        variant_id=instance.variant_id,
                        policy=instance.policy,
                        required_bytes=max(1, variant.memory_estimate_bytes),
                        state=PlacementState.REQUESTED,
                    )
                    session.add(decision)
                    await session.flush()
                    instance.placement_decision_id = decision.id
                decision_id = decision.id

            while True:
                async with session_scope() as session:
                    decision = await session.get(PlacementDecisionRow, decision_id)
                    if decision is None:
                        raise RuntimeError("instance placement decision disappeared")
                    state = decision.state
                    if state in {PlacementState.REFUSED, PlacementState.FAILED}:
                        await transition(
                            session,
                            instance_id,
                            InstanceState.FAILED,
                            reason=decision.refusal_detail or "placement failed",
                            failure_code=decision.refusal_code or "placement_failed",
                        )
                        # Placement can fail after creating a pending/held reservation for
                        # this instance.  A terminal placement decision must release that
                        # reservation or it will poison subsequent admissions after restart.
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
                        failures.add(1, {"reason": decision.refusal_code or "placement_failed"})
                        return
                    if state is PlacementState.LOADING:
                        instance = await session.get(ModelInstanceRow, instance_id)
                        if instance is not None and instance.state is InstanceState.RESERVING:
                            await transition(
                                session,
                                instance_id,
                                InstanceState.LAUNCHING,
                                reason="engine command accepted",
                            )
                    if state is PlacementState.READY:
                        break
                await asyncio.sleep(settings.placement_poll_interval_s)

            async with session_scope() as session:
                instance = await session.get(ModelInstanceRow, instance_id)
                decision = await session.get(PlacementDecisionRow, decision_id)
                if instance is None or decision is None or decision.selected_node_id is None:
                    raise RuntimeError("ready placement has no selected node")
                reservation = await session.scalar(
                    select(MemoryReservationRow).where(
                        MemoryReservationRow.node_id == decision.selected_node_id,
                        MemoryReservationRow.holder_type == ReservationHolder.MODEL,
                        MemoryReservationRow.holder_id.in_(
                            [str(instance.model_id), str(instance.id)]
                        ),
                        MemoryReservationRow.state == MemoryReservationState.HELD,
                    )
                )
                engine = await session.scalar(
                    select(EngineProcessRow)
                    .where(
                        EngineProcessRow.node_id == decision.selected_node_id,
                        EngineProcessRow.model_id == instance.model_id,
                        EngineProcessRow.state == EngineState.READY,
                    )
                    .order_by(EngineProcessRow.started_at.desc())
                    .limit(1)
                )
                node = await session.get(NodeRow, decision.selected_node_id)
                if reservation is None or engine is None or node is None:
                    raise RuntimeError("ready placement is missing reservation, engine, or node")
                reservation.holder_id = str(instance.id)
                engine.instance_id = instance.id
                member = await session.scalar(
                    select(InstanceMemberRow).where(
                        InstanceMemberRow.instance_id == instance.id,
                        InstanceMemberRow.rank == 0,
                    )
                )
                if member is None:
                    session.add(
                        InstanceMemberRow(
                            instance_id=instance.id,
                            node_id=node.id,
                            rank=0,
                            engine_id=engine.id,
                            reservation_id=reservation.id,
                            host=node.control_host or node.name,
                            port=engine.port,
                        )
                    )
                if instance.state is InstanceState.RESERVING:
                    await transition(
                        session, instance_id, InstanceState.LAUNCHING, reason="engine launched"
                    )
                await transition(
                    session, instance_id, InstanceState.WARMING, reason="engine health warming"
                )
                await transition(session, instance_id, InstanceState.READY, reason="engine ready")
        except Exception as exc:
            span.record_exception(exc)
            async with session_scope() as session:
                instance = await session.get(ModelInstanceRow, instance_id)
                # A scheduler restart can replay this step while the node is still
                # re-registering its engine. Keep the durable launch state recoverable;
                # placement failures already transition the instance explicitly above,
                # while this transient gap must be retried by DBOS.
                recovering = instance is not None and instance.state in {
                    InstanceState.RESERVING,
                    InstanceState.LAUNCHING,
                    InstanceState.WARMING,
                }
                if (
                    instance is not None
                    and instance.state
                    not in {
                        InstanceState.STOPPED,
                        InstanceState.FAILED,
                    }
                    and not recovering
                ):
                    await transition(
                        session,
                        instance_id,
                        InstanceState.FAILED,
                        reason="instance launch failed; inspect correlated logs",
                        failure_code=type(exc).__name__.lower()[:64],
                    )
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
                    failures.add(1, {"reason": type(exc).__name__.lower()[:64]})
            raise


@DBOS.workflow(name="coire.instance.launch", max_recovery_attempts=100)
async def instance_launch_workflow(instance_id: str) -> None:
    await execute_instance_launch(instance_id)


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1.0)
async def execute_instance_drain(instance_id_text: str) -> None:
    instance_id = uuid.UUID(instance_id_text)
    settings = get_settings()
    async with session_scope() as session:
        candidate = await session.get(ModelInstanceRow, instance_id)
        sharded = candidate is not None and candidate.policy.startswith("sharded:")
    if sharded:
        from coire_scheduler.sharded_instances import drain_sharded

        await drain_sharded(instance_id)
        return
    command_id: uuid.UUID | None = None
    while command_id is None:
        async with session_scope() as session:
            instance = await session.get(ModelInstanceRow, instance_id)
            if instance is None or instance.state in {
                InstanceState.STOPPED,
                InstanceState.FAILED,
            }:
                return
            member = await session.scalar(
                select(InstanceMemberRow).where(InstanceMemberRow.instance_id == instance_id)
            )
            if member is None or member.reservation_id is None:
                await transition(
                    session, instance_id, InstanceState.FAILED, reason="instance member disappeared"
                )
                return
            active = await session.scalar(
                select(func.count(RequestLeaseRow.id)).where(
                    RequestLeaseRow.reservation_id == member.reservation_id,
                    RequestLeaseRow.released_at.is_(None),
                    RequestLeaseRow.expires_at > datetime.now(UTC),
                )
            )
            expired = (
                instance.drain_deadline is not None and datetime.now(UTC) >= instance.drain_deadline
            )
            if not active or expired:
                reservation = await session.get(MemoryReservationRow, member.reservation_id)
                engine = (
                    await session.get(EngineProcessRow, member.engine_id)
                    if member.engine_id is not None
                    else None
                )
                if reservation is None or engine is None:
                    await transition(
                        session,
                        instance_id,
                        InstanceState.FAILED,
                        reason="drain target disappeared",
                    )
                    return
                if expired:
                    leases = (
                        (
                            await session.execute(
                                select(RequestLeaseRow).where(
                                    RequestLeaseRow.reservation_id == reservation.id,
                                    RequestLeaseRow.released_at.is_(None),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for lease in leases:
                        lease.released_at = datetime.now(UTC)
                decision_id = uuid.uuid5(uuid.NAMESPACE_URL, f"coire:instance-drain:{instance_id}")
                decision = await session.get(PlacementDecisionRow, decision_id)
                if decision is None:
                    decision = PlacementDecisionRow(
                        id=decision_id,
                        model_id=instance.model_id,
                        variant_id=instance.variant_id,
                        policy="instance-drain",
                        required_bytes=reservation.bytes,
                        state=PlacementState.EVICTING,
                        selected_node_id=member.node_id,
                        evicted_reservation_ids=[str(reservation.id)],
                    )
                    session.add(decision)
                    await session.flush()
                command_id = uuid.uuid5(
                    uuid.NAMESPACE_URL, f"coire:instance-drain-command:{instance_id}"
                )
                if await session.get(PlacementCommandRow, command_id) is None:
                    session.add(
                        PlacementCommandRow(
                            id=command_id,
                            decision_id=decision_id,
                            node_id=member.node_id,
                            reservation_id=reservation.id,
                            engine_id=engine.id,
                            operation="unload",
                        )
                    )
                reservation.state = MemoryReservationState.RELEASING
        await asyncio.sleep(settings.placement_poll_interval_s)

    while True:
        async with session_scope() as session:
            command = await session.get(PlacementCommandRow, command_id)
            if command is None:
                raise RuntimeError("instance drain command disappeared")
            if command.state == "succeeded":
                await transition(
                    session, instance_id, InstanceState.STOPPED, reason="drain completed"
                )
                return
            if command.state == "failed":
                await transition(
                    session,
                    instance_id,
                    InstanceState.FAILED,
                    reason=command.failure_detail or "drain failed",
                    failure_code=command.failure_code,
                )
                return
        await asyncio.sleep(settings.placement_poll_interval_s)


@DBOS.workflow(name="coire.instance.drain", max_recovery_attempts=100)
async def instance_drain_workflow(instance_id: str) -> None:
    await execute_instance_drain(instance_id)
