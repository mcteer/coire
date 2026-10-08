"""Atomic full-envelope admission using the existing ordered placement locks."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    AuditRow,
    EngineProcessRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelRow,
    NodeMemoryLedgerRow,
    NodeRow,
    PlacementCommandRow,
    PlacementDecisionRow,
    RequestLeaseRow,
    TrainingAttemptRow,
    TrainingCommandRow,
    TrainingEvictionIntentRow,
    TrainingJobRow,
    TrainingParticipantRow,
    TrainingStorageReservationRow,
)
from coire_api.placement.service import effective_occupied_bytes, lock_nodes_for_admission
from coire_api.training.authorization import authorize_live_training_action
from coire_api.training.service import recheck_training_inputs, training_id
from coire_api.training.telemetry import observed
from coire_core.errors import TrainingConflict
from coire_core.models.adapters import InferenceTarget
from coire_core.models.engine import EngineState
from coire_core.models.instance import InstanceState
from coire_core.models.node import Reachability
from coire_core.models.placement import MemoryReservationState, PlacementState, ReservationHolder
from coire_core.models.training import (
    TrainingProfile,
    parse_resolved_training_spec,
)


async def admission_state(
    session: AsyncSession, job: TrainingJobRow, state: str, reason: str | None
) -> None:
    from coire_api.training.events import append_event
    from coire_core.models.training import TrainingStateEvent

    if (job.state, job.safe_reason) == (state, reason):
        return
    job.state, job.safe_reason = state, reason
    job.version += 1
    job.updated_at = datetime.now(UTC)
    if state == "failed":
        job.finished_at = job.updated_at
    await append_event(
        session,
        job.id,
        TrainingStateEvent.model_validate(
            {
                "kind": "terminal" if state == "failed" else "state",
                "state": state,
                "reason": reason,
            }
        ),
    )


COUNTED = (
    MemoryReservationState.PENDING,
    MemoryReservationState.HELD,
    MemoryReservationState.RELEASING,
)


@dataclass
class TrainingVictim:
    hold: MemoryReservationRow
    instance: ModelInstanceRow
    engine: EngineProcessRow
    target: InferenceTarget


async def restoration_version(
    session: AsyncSession, instance: ModelInstanceRow, hold: MemoryReservationRow
) -> str:
    """Freeze policy/target/pin plus audited mutation identity, not lifecycle timestamps."""
    model = await session.get(ModelRow, instance.model_id)
    audit = await session.scalar(
        select(AuditRow.id)
        .where(
            AuditRow.target_id.in_([str(instance.id), str(hold.id), str(instance.model_id)]),
            AuditRow.action.not_in(
                ["instance.transition", "training.victim.drain", "training.victim.restore"]
            ),
        )
        .order_by(AuditRow.at.desc(), AuditRow.id.desc())
        .limit(1)
    )
    value = {
        "policy": instance.policy,
        "model_policy": model.placement_policy if model else None,
        "model_state": str(model.state) if model else None,
        "model_updated": model.updated_at.isoformat() if model else None,
        "variant": str(instance.variant_id),
        "adapter": str(instance.adapter_id),
        "pinned": hold.pinned,
        "mutation": str(audit),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def eligible_victims(
    session: AsyncSession, holds: list[MemoryReservationRow]
) -> list[TrainingVictim]:
    from coire_api.auth import ADMIN
    from coire_api.db import TrainingAdapterRow
    from coire_api.gateway.targets import ModelNotFoundError, resolve_target

    victims = []
    now = datetime.now(UTC)
    for hold in sorted(holds, key=lambda item: item.last_used_at):
        if (
            hold.holder_type != ReservationHolder.MODEL
            or hold.pinned
            or hold.state != MemoryReservationState.HELD
        ):
            continue
        member = await session.scalar(
            select(InstanceMemberRow).where(InstanceMemberRow.reservation_id == hold.id)
        )
        instance = (
            await session.get(
                ModelInstanceRow, member.instance_id, populate_existing=True, with_for_update=True
            )
            if member
            else None
        )
        if (
            instance is None
            or instance.state != InstanceState.READY
            or not instance.policy.startswith("single:")
            or instance.in_flight
            or instance.variant_id is None
        ):
            continue
        members = (
            await session.scalars(
                select(InstanceMemberRow).where(InstanceMemberRow.instance_id == instance.id)
            )
        ).all()
        if (
            len(members) != 1
            or await session.scalar(
                select(RequestLeaseRow.id)
                .where(
                    RequestLeaseRow.reservation_id == hold.id,
                    RequestLeaseRow.released_at.is_(None),
                    RequestLeaseRow.expires_at > now,
                )
                .limit(1)
            )
            is not None
        ):
            continue
        model = await session.get(ModelRow, instance.model_id)
        engine = (
            await session.get(EngineProcessRow, member.engine_id)
            if member and member.engine_id
            else None
        )
        if (
            model is None
            or model.idle_ttl_seconds is None
            or engine is None
            or engine.state != EngineState.READY
            or engine.instance_id != instance.id
            or engine.node_id != hold.node_id
        ):
            continue
        adapter = (
            await session.get(TrainingAdapterRow, instance.adapter_id)
            if instance.adapter_id
            else None
        )
        try:
            target = await resolve_target(
                session,
                adapter.selector if adapter else instance.model_id,
                ADMIN,
                instance.variant_id,
            )
        except (LookupError, ModelNotFoundError):
            continue
        if target.identity is None:
            continue
        victims.append(TrainingVictim(hold, instance, engine, target.identity))
    return victims


async def persist_victim_drains(
    session: AsyncSession,
    attempt: TrainingAttemptRow,
    job: TrainingJobRow,
    victims: list[TrainingVictim],
) -> None:
    from coire_api.instance.service import transition

    if not victims:
        return
    decision_id = uuid.uuid5(uuid.NAMESPACE_URL, f"coire:training-drain:{attempt.id}")
    session.add(
        PlacementDecisionRow(
            id=decision_id,
            model_id=job.model_id,
            variant_id=job.base_variant_id,
            policy="training-drain",
            required_bytes=parse_resolved_training_spec(
                job.resolved_spec
            ).resource_envelope.memory_bytes,
            state=PlacementState.EVICTING,
            evicted_reservation_ids=[str(victim.hold.id) for victim in victims],
        )
    )
    await session.flush()
    for victim in victims:
        version = await restoration_version(session, victim.instance, victim.hold)
        session.add(
            TrainingEvictionIntentRow(
                id=uuid.uuid5(decision_id, str(victim.hold.id)),
                attempt_id=attempt.id,
                instance_id=victim.instance.id,
                reservation_id=victim.hold.id,
                target=victim.target.model_dump(mode="json"),
                prior_policy=victim.instance.policy,
                prior_version=version,
                restoration_state="pending",
            )
        )
        await transition(
            session, victim.instance.id, InstanceState.DRAINING, reason="training idle eviction"
        )
        victim.hold.state = MemoryReservationState.RELEASING
        session.add(
            PlacementCommandRow(
                id=uuid.uuid5(decision_id, str(victim.hold.id)),
                decision_id=decision_id,
                node_id=victim.hold.node_id,
                reservation_id=victim.hold.id,
                engine_id=victim.engine.id,
                operation="unload",
                state="pending",
            )
        )


async def victim_drain_barrier(session: AsyncSession, attempt_id: str) -> bool:
    victims = (
        await session.scalars(
            select(TrainingEvictionIntentRow).where(
                TrainingEvictionIntentRow.attempt_id == attempt_id
            )
        )
    ).all()
    for victim in victims:
        hold = await session.get(
            MemoryReservationRow, victim.reservation_id, populate_existing=True
        )
        instance = await session.get(ModelInstanceRow, victim.instance_id, populate_existing=True)
        command = await session.get(PlacementCommandRow, victim.id, populate_existing=True)
        if (
            hold is None
            or hold.state != MemoryReservationState.RELEASED
            or instance is None
            or instance.state != InstanceState.STOPPED
            or command is None
            or command.state != "succeeded"
        ):
            return False
    return True


@observed("coire.scheduler.training.admit")
async def admit_training(
    session: AsyncSession, job: TrainingJobRow, nodes: list[NodeRow]
) -> TrainingAttemptRow | None:
    """All ranks get full weights and optimizer holds, or none do.

    Unmeasured coexistence is deliberately queued. No inference victim is
    optimistically subtracted while its engine remains alive.
    """
    await authorize_live_training_action(
        session, Principal.model_validate(job.authorization_snapshot)
    )
    refreshed = await session.get(
        TrainingJobRow, job.id, populate_existing=True, with_for_update=True
    )
    if refreshed is None or refreshed.state not in {"queued", "recovering"}:
        raise TrainingConflict("Job is not awaiting admission")
    job = refreshed
    if (
        await session.scalar(
            select(TrainingAttemptRow.id).where(
                TrainingAttemptRow.job_id == job.id,
                TrainingAttemptRow.state.in_(["preparing", "running", "stopping", "unknown"]),
            )
        )
        is not None
    ):
        await admission_state(session, job, job.state, "node_unreachable")
        return None
    if job.resolved_spec is None:
        raise TrainingConflict("Training requires measured full-envelope preflight")
    resolved = parse_resolved_training_spec(job.resolved_spec)
    await recheck_training_inputs(session, resolved)
    expected = 2 if resolved.spec.placement.mode == "data_parallel" else 1
    if len(nodes) != expected or len({n.id for n in nodes}) != expected:
        raise TrainingConflict("Admission participants differ from the immutable world size")
    await lock_nodes_for_admission(session, [n.id for n in nodes])
    now = datetime.now(UTC)
    if job.queue_deadline_at <= now:
        await admission_state(session, job, "failed", "queue_timeout")
        return None
    from coire_scheduler.training_guard import recheck_resource_evidence

    evidence_reason = await recheck_resource_evidence(session, job, nodes)
    if evidence_reason is not None:
        await admission_state(session, job, job.state, evidence_reason)
        return None
    if expected == 2:
        from coire_api.sharding import link_projection
        from coire_core.settings import get_settings

        if not (await link_projection(session, get_settings())).tp_eligible:
            await admission_state(session, job, job.state, "node_unreachable")
            return None
    victims: list[TrainingVictim] = []
    selected_profile: TrainingProfile | None = None
    from coire_scheduler.training_guard import (
        current_residents,
        instance_latency_reason,
        matching_profile,
    )

    all_holds: dict[uuid.UUID, list[MemoryReservationRow]] = {}
    for node in nodes:
        ledger = await session.get(
            NodeMemoryLedgerRow, node.id, populate_existing=True, with_for_update=True
        )
        if (
            node.name not in {"coire-edge-a", "coire-edge-b"}
            or ledger is None
            or ledger.health is not Reachability.HEALTHY
            or ledger.health_sampled_at is None
            or now - ledger.health_sampled_at > timedelta(seconds=60)
            or ledger.thermal_state not in {"nominal", "fair"}
        ):
            await admission_state(session, job, job.state, "node_unreachable")
            return None
        reservations = list(
            (
                await session.scalars(
                    select(MemoryReservationRow)
                    .where(
                        MemoryReservationRow.node_id == node.id,
                        MemoryReservationRow.state.in_(COUNTED),
                    )
                    .with_for_update()
                )
            ).all()
        )
        fixed = sum(r.bytes for r in reservations if r.holder_type is ReservationHolder.SANDBOX)
        needed = resolved.resource_envelope.memory_bytes
        if needed + fixed > ledger.budget_bytes:
            await admission_state(session, job, "failed", "impossible_fit")
            return None
        all_holds[node.id] = reservations
        if any(
            r.holder_type
            in {
                ReservationHolder.IMAGE,
                ReservationHolder.TRAINING,
                ReservationHolder.CONVERSION,
            }
            for r in reservations
        ):
            await admission_state(session, job, job.state, "capacity_busy")
            return None
    # Exact measured coexistence is preferred; otherwise drain every eligible
    # idle single-node resident and require evidence for the protected remainder.
    residents = await current_residents(session, [node.id for node in nodes])
    if residents is not None:
        selected_profile = await matching_profile(session, job, nodes, residents=residents)
    if selected_profile is None:
        for node in nodes:
            victims.extend(await eligible_victims(session, all_holds[node.id]))
        residents = await current_residents(
            session, [node.id for node in nodes], exclude=[victim.hold.id for victim in victims]
        )
        if residents is not None:
            selected_profile = await matching_profile(session, job, nodes, residents=residents)
    if selected_profile is None:
        await admission_state(session, job, job.state, "profile_missing")
        return None
    for resident in residents or []:
        reason = await instance_latency_reason(session, resident)
        if reason is not None:
            await admission_state(session, job, job.state, reason)
            return None
    for node in nodes:
        ledger = await session.get(
            NodeMemoryLedgerRow, node.id, populate_existing=True, with_for_update=True
        )
        assert ledger is not None
        reservations = all_holds[node.id]
        occupied = effective_occupied_bytes(reservations, ledger.measured_resident_bytes)
        future_occupied = (
            occupied
            - sum(victim.hold.bytes for victim in victims if victim.hold.node_id == node.id)
            if occupied is not None
            else None
        )
        if future_occupied is None or future_occupied + needed > ledger.budget_bytes:
            await admission_state(session, job, job.state, "capacity_busy")
            return None
        checkpoint_bytes = resolved.resource_envelope.checkpoint_bytes
        disk_holds = list(
            (
                await session.scalars(
                    select(TrainingStorageReservationRow)
                    .where(
                        TrainingStorageReservationRow.node_id == node.id,
                        TrainingStorageReservationRow.state.in_(["held", "releasing"]),
                    )
                    .with_for_update()
                )
            ).all()
        )
        if (
            checkpoint_bytes * 2 > 20 * 1024**3
            or sum(h.bytes for h in disk_holds) + checkpoint_bytes * 3 > 200 * 1024**3
        ):
            await admission_state(session, job, job.state, "disk_full")
            return None
    job.fence += 1
    attempt = TrainingAttemptRow(
        id=training_id(),
        job_id=job.id,
        generation=job.fence,
        fence=job.fence,
        world_size=expected,
        runtime_sha256=resolved.runtime_sha256,
        resume_checkpoint_id=job.latest_checkpoint_id,
        state="preparing",
        lease_expires_at=now + timedelta(seconds=30),
    )
    session.add(attempt)
    await session.flush()
    await persist_victim_drains(session, attempt, job, victims)
    swaps = {
        str(node.id): getattr(
            await session.get(NodeMemoryLedgerRow, node.id), "swap_used_bytes", None
        )
        for node in nodes
    }
    evidence_payload = {"profile_id": str(selected_profile.id), "swap_bytes": swaps}
    session.add(
        TrainingCommandRow(
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"coire:admission-evidence:{attempt.id}"),
            actor_user_id=job.owner_user_id,
            idempotency_key=f"admission-evidence:{attempt.id}",
            operation="training.admission.evidence",
            subject_id=attempt.id,
            job_id=job.id,
            attempt_id=attempt.id,
            payload=evidence_payload,
            request_sha256=hashlib.sha256(
                json.dumps(evidence_payload, sort_keys=True).encode()
            ).hexdigest(),
            state="succeeded",
        )
    )
    for rank, node in enumerate(sorted(nodes, key=lambda n: n.name)):
        hold = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node.id,
            holder_type=ReservationHolder.TRAINING,
            holder_id=attempt.id,
            bytes=resolved.resource_envelope.memory_bytes,
            pinned=True,
            state=MemoryReservationState.PENDING,
        )
        disk = TrainingStorageReservationRow(
            id=uuid.uuid4(),
            owner_user_id=job.owner_user_id,
            node_id=node.id,
            subject_id=attempt.id,
            bytes=resolved.resource_envelope.checkpoint_bytes * 3,
            state="held",
        )
        session.add_all([hold, disk])
        await session.flush()
        session.add(
            TrainingParticipantRow(
                attempt_id=attempt.id,
                node_id=node.id,
                rank=rank,
                reservation_id=hold.id,
                disk_reservation_id=disk.id,
                command_id=uuid.uuid4(),
                request_sha256=job.resolved_sha256,
                spawn_nonce=uuid.uuid4(),
            )
        )
    job.state, job.safe_reason = "reserving", None
    job.version += 1
    job.updated_at = now
    await session.flush()
    return attempt
