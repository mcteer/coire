"""Serialize exact evaluation phases with shared Studio ledger locks and serving priority."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    EvaluationAttemptRow,
    EvaluationMeasurementRow,
    EvaluationRunRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelVariantRow,
    NodeMemoryLedgerRow,
    NodeRow,
    TrainingArtifactCopyRow,
    VariantCopyRow,
)
from coire_api.evaluation.evidence import reserve_quota
from coire_api.instance.service import append_initial_transition
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.placement.service import (
    acquire_lease,
    effective_occupied_bytes,
    lock_nodes_for_admission,
)
from coire_api.registry.visual_memory import reservation_bytes
from coire_core.models.evaluation import (
    EvaluationIdentityRequest,
    EvaluationResident,
    EvaluationSuite,
    EvaluationTarget,
    EvaluationWorkload,
)
from coire_core.models.evaluation_inputs import training_scan_memory_bytes
from coire_core.models.instance import InstanceState
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.settings import Settings
from coire_scheduler.evaluation_guard import (
    COUNTED,
    controlled_measurement,
    fingerprint,
    matching_profile,
    resident_engine_bindings,
)
from coire_scheduler.training_guard import current_residents


async def reserve_phase(
    session: AsyncSession,
    run: EvaluationRunRow,
    attempt: EvaluationAttemptRow,
    target: EvaluationTarget,
    settings: Settings,
) -> bool:
    workload = EvaluationWorkload.model_validate(attempt.workload)
    sandbox_bytes = max(
        settings.run_default_memory_bytes, training_scan_memory_bytes(workload.training)
    )
    if sandbox_bytes > settings.run_max_memory_bytes:
        return False
    await session.execute(text("SELECT pg_advisory_xact_lock(170100)"))
    if attempt.sandbox_reservation_id is not None:
        return True
    active = await session.scalar(
        select(EvaluationAttemptRow.id)
        .where(
            EvaluationAttemptRow.sandbox_reservation_id.is_not(None),
            EvaluationAttemptRow.state != "released",
            EvaluationAttemptRow.id != attempt.id,
        )
        .limit(1)
    )
    if active is not None:
        return False
    measuring = await session.scalar(
        select(EvaluationMeasurementRow.id)
        .where(
            EvaluationMeasurementRow.state == "running",
            EvaluationMeasurementRow.id != run.measurement_id
            if run.measurement_id
            else EvaluationMeasurementRow.id.is_not(None),
        )
        .limit(1)
    )
    if measuring is not None:
        return False
    if not settings.evaluations_enabled:
        return False
    variant = await session.get(ModelVariantRow, target.target.variant_id, populate_existing=True)
    if variant is None:
        return False
    nodes = (
        await session.scalars(
            select(NodeRow)
            .join(VariantCopyRow, VariantCopyRow.node_id == NodeRow.id)
            .where(
                VariantCopyRow.variant_id == variant.id,
                VariantCopyRow.verified.is_(True),
                VariantCopyRow.manifest_sha256 == target.target.base_manifest_sha256,
                NodeRow.role == NodeRole.STUDIO,
                NodeRow.reachability == Reachability.HEALTHY,
            )
            .order_by(NodeRow.name)
        )
    ).all()
    await lock_nodes_for_admission(session, [node.id for node in nodes])
    for node in nodes:
        now = datetime.now(UTC)
        ledger = await session.get(NodeMemoryLedgerRow, node.id, populate_existing=True)
        if (
            ledger is None
            or ledger.health is not Reachability.HEALTHY
            or ledger.health_sampled_at is None
            or not now - timedelta(seconds=60) <= ledger.health_sampled_at <= now
            or ledger.swap_used_bytes is None
            or ledger.swap_used_bytes > 0
            or ledger.thermal_state not in {"nominal", "fair"}
        ):
            continue
        holds = (
            await session.scalars(
                select(MemoryReservationRow).where(
                    MemoryReservationRow.node_id == node.id, MemoryReservationRow.state.in_(COUNTED)
                )
            )
        ).all()
        if any(hold.holder_type is ReservationHolder.TRAINING for hold in holds):
            continue
        residents = await current_residents(session, [node.id])
        if residents is None:
            continue
        projected = [
            EvaluationResident(instance_id=item.instance_id, target=item.target)
            for item in residents
        ]
        selected_profile = None
        if projected:
            bindings = await resident_engine_bindings(session, projected)
            if bindings is None:
                continue
            suite = EvaluationSuite.model_validate(run.suite_snapshot)
            subjects = [EvaluationTarget.model_validate(item) for item in run.subjects]
            selected_profile = await matching_profile(
                session,
                fingerprint(
                    suite,
                    subjects,
                    projected,
                    node,
                    bindings,
                    training=workload.training,
                ),
                now=now,
            )
            if selected_profile is None and not await controlled_measurement(session, run, node):
                continue
        ready = await session.scalar(
            select(ModelInstanceRow)
            .join(InstanceMemberRow, InstanceMemberRow.instance_id == ModelInstanceRow.id)
            .where(
                ModelInstanceRow.variant_id == variant.id,
                ModelInstanceRow.adapter_id == target.target.adapter_id,
                ModelInstanceRow.state == InstanceState.READY,
                InstanceMemberRow.node_id == node.id,
                InstanceMemberRow.rank == 0,
                ModelInstanceRow.policy.not_like("sharded:%"),
            )
            .limit(1)
        )
        adapter_bytes = (
            int(
                await session.scalar(
                    select(func.max(TrainingArtifactCopyRow.total_bytes)).where(
                        TrainingArtifactCopyRow.adapter_id == target.target.adapter_id,
                        TrainingArtifactCopyRow.state == "verified",
                    )
                )
                or 0
            )
            if target.target.adapter_id
            else 0
        )
        occupied = effective_occupied_bytes(holds, ledger.measured_resident_bytes)
        needed = sandbox_bytes + (
            0
            if ready
            else reservation_bytes(variant.memory_estimate_bytes, variant.visual_capability)
            + adapter_bytes
        )
        if occupied is None or occupied + needed > ledger.budget_bytes:
            continue
        if target.variant_slug is None:
            continue
        try:
            async with NodeClient(settings) as client:
                identity = await client.evaluation_identity(
                    node.name,
                    EvaluationIdentityRequest(
                        engine_backend=target.engine_backend,
                        target=target.target,
                        variant_slug=target.variant_slug,
                        template_override=target.template_override,
                        capability_profile=target.capability_profile,
                    ),
                )
            if identity != target.runtime:
                continue
        except NodeError:
            continue
        await reserve_quota(session, run, settings)
        leases: list[str] = []
        for resident in projected:
            member = await session.scalar(
                select(InstanceMemberRow).where(
                    InstanceMemberRow.instance_id == resident.instance_id
                )
            )
            if member is None or member.reservation_id is None:
                raise RuntimeError(
                    "evaluation resident reservation disappeared under admission lock"
                )
            lease = await acquire_lease(
                session,
                member.reservation_id,
                f"evaluation-resident:{attempt.id}",
                ttl_seconds=max(1, (attempt.deadline_at - now).total_seconds()),
            )
            leases.append(str(lease.id))
        hold = MemoryReservationRow(
            id=uuid.uuid5(attempt.id, "sandbox"),
            node_id=node.id,
            holder_type=ReservationHolder.RUN,
            holder_id=str(attempt.id),
            bytes=sandbox_bytes,
            pinned=True,
            state=MemoryReservationState.HELD,
            last_used_at=now,
        )
        session.add(hold)
        await session.flush()
        if ready is None:
            ready = ModelInstanceRow(
                id=uuid.uuid5(attempt.id, "engine"),
                model_id=target.target.model_id,
                variant_id=target.target.variant_id,
                adapter_id=target.target.adapter_id,
                policy=f"single:{node.name}",
                state=InstanceState.REQUESTED,
            )
            session.add(ready)
            await session.flush()
            await append_initial_transition(session, ready)
            attempt.owns_instance = True
        attempt.instance_id, attempt.node_id, attempt.sandbox_reservation_id = (
            ready.id,
            node.id,
            hold.id,
        )
        attempt.state = "reserving"
        attempt.profile_id = selected_profile.id if selected_profile else None
        attempt.resident_lease_ids = leases
        run.cleanup_state = "pending"
        return True
    return False
