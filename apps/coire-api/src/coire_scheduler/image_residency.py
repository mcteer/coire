"""Durable idle unload of image workers with exact node stop proof."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from opentelemetry import metrics, trace
from sqlalchemy import func, select

from coire_api.audit import write_audit
from coire_api.db import (
    ImageExecutionLeaseRow,
    ImageJobRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    NodeRow,
    session_scope,
)
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.placement.service import node_admission_lock
from coire_core.models.audit import AuditOutcome
from coire_core.models.image_worker import ImageWorkerUnloadRequest
from coire_core.models.images import ImageJobState
from coire_core.models.instance import InstanceState
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.settings import Settings

_BATCH = 25
_scan_after: uuid.UUID | None = None
_TERMINAL = (ImageJobState.SUCCEEDED, ImageJobState.FAILED, ImageJobState.CANCELLED)
logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.scheduler.image_residency")
unloads_total = metrics.get_meter("coire.scheduler.image_residency").create_counter(
    "coire_image_worker_idle_unloads_total", unit="1"
)


def image_idle_due(last_used: datetime, *, now: datetime, ttl_seconds: int) -> bool:
    """The worker is idle only after the complete configured interval."""
    return last_used + timedelta(seconds=ttl_seconds) <= now


async def _prepare_idle_unload(
    reservation_id: uuid.UUID, settings: Settings, now: datetime
) -> tuple[str, uuid.UUID, uuid.UUID] | None:
    async with session_scope() as session:
        reservation = await session.get(MemoryReservationRow, reservation_id)
        if (
            reservation is None
            or reservation.holder_type is not ReservationHolder.IMAGE
            or reservation.state is not MemoryReservationState.HELD
            or reservation.pinned
        ):
            return None
        async with node_admission_lock(session, reservation.node_id):
            reservation = await session.get(
                MemoryReservationRow, reservation_id, populate_existing=True, with_for_update=True
            )
            if (
                reservation is None
                or reservation.state is not MemoryReservationState.HELD
                or reservation.pinned
            ):
                return None
            try:
                instance_id = uuid.UUID(reservation.holder_id)
            except ValueError:
                return None
            instance = await session.get(
                ModelInstanceRow, instance_id, populate_existing=True, with_for_update=True
            )
            if (
                instance is None
                or not instance.policy.startswith("image:")
                or instance.state
                not in {
                    InstanceState.LAUNCHING,
                    InstanceState.WARMING,
                    InstanceState.READY,
                    InstanceState.FAILED,
                    InstanceState.DRAINING,
                }
            ):
                return None
            member = await session.scalar(
                select(InstanceMemberRow)
                .where(
                    InstanceMemberRow.instance_id == instance_id,
                    InstanceMemberRow.node_id == reservation.node_id,
                )
                .limit(1)
            )
            node = await session.get(NodeRow, reservation.node_id) if member is not None else None
            if node is None or instance.policy != f"image:{node.name}":
                return None
            active_job = await session.scalar(
                select(ImageJobRow.id)
                .where(
                    ImageJobRow.instance_id == instance_id,
                    ImageJobRow.state.notin_(_TERMINAL),
                )
                .limit(1)
            )
            active_lease = await session.scalar(
                select(ImageExecutionLeaseRow.id)
                .where(
                    ImageExecutionLeaseRow.node_id == reservation.node_id,
                    ImageExecutionLeaseRow.mode == "image",
                    ImageExecutionLeaseRow.released_at.is_(None),
                )
                .limit(1)
            )
            if active_job is not None or active_lease is not None:
                return None
            if instance.state is not InstanceState.DRAINING:
                latest_job = await session.scalar(
                    select(func.max(ImageJobRow.updated_at)).where(
                        ImageJobRow.instance_id == instance_id
                    )
                )
                last_used = max(
                    reservation.last_used_at,
                    instance.updated_at,
                    *(item for item in (latest_job,) if item is not None),
                )
                if not image_idle_due(
                    last_used, now=now, ttl_seconds=settings.image_worker_idle_ttl_s
                ):
                    return None
                instance.state = InstanceState.DRAINING
                instance.updated_at = now
                instance.transitioned_at = now
                await write_audit(
                    session,
                    actor="coire-scheduler",
                    action="image.worker.idle_unload_requested",
                    target_type="image_worker",
                    target_id=str(instance_id),
                    outcome=AuditOutcome.OK,
                    context={"node_id": str(node.id)},
                )
            return node.name, node.id, instance_id


async def _confirm_idle_unload(
    reservation_id: uuid.UUID, instance_id: uuid.UUID, node_id: uuid.UUID
) -> bool:
    async with session_scope() as session, node_admission_lock(session, node_id):
        reservation = await session.get(
            MemoryReservationRow, reservation_id, populate_existing=True, with_for_update=True
        )
        instance = await session.get(
            ModelInstanceRow, instance_id, populate_existing=True, with_for_update=True
        )
        if (
            reservation is None
            or reservation.node_id != node_id
            or reservation.holder_type is not ReservationHolder.IMAGE
            or reservation.holder_id != str(instance_id)
            or reservation.state is not MemoryReservationState.HELD
            or instance is None
            or instance.state is not InstanceState.DRAINING
        ):
            return False
        now = datetime.now(UTC)
        reservation.state = MemoryReservationState.RELEASED
        reservation.released_at = now
        instance.state = InstanceState.STOPPED
        instance.updated_at = now
        instance.transitioned_at = now
        await write_audit(
            session,
            actor="coire-scheduler",
            action="image.worker.idle_unloaded",
            target_type="image_worker",
            target_id=str(instance_id),
            outcome=AuditOutcome.OK,
            context={"node_id": str(node_id)},
        )
        return True


async def sweep_idle_image_workers(settings: Settings) -> int:
    """Try a bounded batch; a missing node reply leaves the durable hold draining."""
    global _scan_after
    async with session_scope() as session:
        query = (
            select(MemoryReservationRow.id)
            .where(
                MemoryReservationRow.holder_type == ReservationHolder.IMAGE,
                MemoryReservationRow.state == MemoryReservationState.HELD,
                MemoryReservationRow.pinned.is_(False),
            )
            .order_by(MemoryReservationRow.id)
            .limit(_BATCH)
        )
        candidates = (
            await session.scalars(
                query.where(MemoryReservationRow.id > _scan_after)
                if _scan_after is not None
                else query
            )
        ).all()
        if not candidates and _scan_after is not None:
            candidates = (await session.scalars(query)).all()
        _scan_after = candidates[-1] if candidates else None
    completed = 0
    for reservation_id in candidates:
        with tracer.start_as_current_span("coire.scheduler.image.idle_unload") as span:
            span.set_attribute("coire.reservation_id", str(reservation_id))
            try:
                prepared = await _prepare_idle_unload(reservation_id, settings, datetime.now(UTC))
                if prepared is None:
                    continue
                node_name, node_id, instance_id = prepared
                async with NodeClient(settings) as client:
                    result = await client.unload_image_worker(
                        node_name,
                        ImageWorkerUnloadRequest(
                            instance_id=instance_id,
                            reason="idle_ttl",
                            requested_at=datetime.now(UTC),
                        ),
                    )
                if (
                    result.instance_id != instance_id
                    or result.state != "failed"
                    or result.reserved_bytes != 0
                    or result.safe_error != "worker_stopped"
                ):
                    raise RuntimeError("image worker stop proof differs from request")
                if not await _confirm_idle_unload(reservation_id, instance_id, node_id):
                    continue
                completed += 1
                unloads_total.add(1, {"outcome": "confirmed"})
            except (NodeError, RuntimeError) as exc:
                unloads_total.add(1, {"outcome": "uncertain"})
                logger.warning(
                    "image idle worker stop unconfirmed",
                    extra={"reservation_id": str(reservation_id), "error_type": type(exc).__name__},
                )
    return completed
