"""Owned-process proofs precede release; timeouts never prove trainer death."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    MemoryReservationRow,
    ModelInstanceRow,
    ModelRow,
    NodeRow,
    TrainingAttemptRow,
    TrainingCommandRow,
    TrainingEvictionIntentRow,
    TrainingMetricRow,
    TrainingParticipantRow,
)
from coire_api.placement.service import lock_nodes_for_admission
from coire_api.training.checkpoints import recovery_checkpoint
from coire_api.training.events import append_event, current_job
from coire_api.training.telemetry import observed
from coire_core.errors import TrainingConflict
from coire_core.models.placement import MemoryReservationState
from coire_core.models.training import TERMINAL_TRAINING_STATES, TrainingStateEvent
from coire_core.models.training_node import TrainingStopReceipt


@observed("coire.scheduler.training.stop_proof")
async def record_stop_proof(
    session: AsyncSession, attempt_id: str, proof: TrainingStopReceipt
) -> bool:
    attempt = await session.get(TrainingAttemptRow, attempt_id)
    if attempt is None:
        raise TrainingConflict("Unknown training attempt")
    participants = list(
        (
            await session.scalars(
                select(TrainingParticipantRow).where(
                    TrainingParticipantRow.attempt_id == attempt_id
                )
            )
        ).all()
    )
    job = await current_job(session, attempt.job_id, lock=True)
    await lock_nodes_for_admission(session, [p.node_id for p in participants])
    attempt = await session.get(
        TrainingAttemptRow, attempt_id, populate_existing=True, with_for_update=True
    )
    assert attempt is not None
    if proof.attempt_id != attempt_id or proof.fence != attempt.fence:
        raise TrainingConflict("Stop proof identifies a different attempt")
    node = await session.scalar(select(NodeRow).where(NodeRow.name == proof.node))
    participant = next((p for p in participants if node is not None and p.node_id == node.id), None)
    if participant is None or not proof.stopped:
        raise TrainingConflict("Stop observation does not prove this participant terminated")
    participant = await session.get(
        TrainingParticipantRow, participant.id, populate_existing=True, with_for_update=True
    )
    assert participant is not None
    learn_identity = participant.pid is None and proof.pid is not None
    if learn_identity:
        from coire_core.models.training_node import TrainingStartRequest

        starts = list(
            (
                await session.scalars(
                    select(TrainingCommandRow).where(
                        TrainingCommandRow.attempt_id == attempt.id,
                        TrainingCommandRow.operation == "node.training.start",
                        TrainingCommandRow.subject_id == proof.node,
                        TrainingCommandRow.state.in_(["dispatching", "succeeded"]),
                    )
                )
            ).all()
        )
        matched = False
        for row in starts:
            try:
                start = TrainingStartRequest.model_validate(row.payload)
            except ValueError:
                continue
            if (
                start.spawn_nonce == participant.spawn_nonce
                and start.job_id == job.id
                and start.attempt_id == attempt.id
                and start.fence == proof.fence
                and start.node == proof.node
                and start.prepared_command_id == participant.command_id
                and start.rank == participant.rank
                and start.world_size == attempt.world_size
            ):
                matched = True
                break
        if not matched:
            raise TrainingConflict("Stop identity has no matching persisted spawn intent")
    elif (
        participant.pid != proof.pid or participant.process_create_time != proof.process_create_time
    ):
        raise TrainingConflict("Stop proof does not match the owned process identity")
    if proof.observed_at > datetime.now(UTC) or proof.observed_at < attempt.created_at:
        raise TrainingConflict("Stop proof observation is outside the attempt lifetime")
    if attempt.state == "stopped":
        if participant.stop_proof is None:
            raise TrainingConflict("Stopped attempt is missing participant proof")
        return True
    if learn_identity:
        participant.pid, participant.process_create_time = proof.pid, proof.process_create_time
    if participant.stop_proof is None:
        participant.stop_proof = proof.model_dump(mode="json")
        participant.stopped_at = proof.observed_at
    await session.flush()
    participants = list(
        (
            await session.scalars(
                select(TrainingParticipantRow)
                .where(TrainingParticipantRow.attempt_id == attempt_id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(participants) != attempt.world_size or any(p.stop_proof is None for p in participants):
        return False
    now = datetime.now(UTC)
    for p in participants:
        reservation = await session.get(
            MemoryReservationRow, p.reservation_id, populate_existing=True, with_for_update=True
        )
        if reservation is None or reservation.holder_id != attempt.id:
            raise TrainingConflict("Training reservation identity changed")
        reservation.state, reservation.released_at = MemoryReservationState.RELEASED, now
        # Disk remains held while retained checkpoint/adapter bytes exist.
    attempt.state, attempt.stopped_at = "stopped", now
    await restore_evictions(session, attempt.id)
    worker_starts = [
        p.process_create_time for p in participants if p.process_create_time is not None
    ]
    if worker_starts:
        # psutil create_time is an epoch timestamp for the owned native worker.
        # Count the union of all-rank active intervals, once, after all proofs.
        first_started = max(attempt.created_at.timestamp(), min(worker_starts))
        job.cumulative_execution_seconds += max(0.0, now.timestamp() - first_started)
    if job.fence == attempt.fence:
        if job.state == "cancelling":
            job.state, job.finished_at = "cancelled", now
        elif job.state == "pausing":
            checkpoint = await recovery_checkpoint(session, job)
            job.state = "paused"
            job.latest_checkpoint_id = checkpoint.id if checkpoint else None
            job.completed_update = checkpoint.completed_update if checkpoint else 0
            await session.execute(
                update(TrainingMetricRow)
                .where(
                    TrainingMetricRow.attempt_id == attempt.id,
                    TrainingMetricRow.completed_update > job.completed_update,
                )
                .values(rolled_back=True)
            )
        elif job.state not in {"succeeded", "failed", "cancelled", "finalizing"}:
            job.state, job.safe_reason = "recovering", "rank_failed"
        job.version += 1
        job.updated_at = now
        if (
            job.state == "paused"
            and job.pause_origin == "evaluation"
            and job.evaluation_pause_trigger_id is not None
        ):
            from coire_api.db import TrainingEvaluationTriggerRow

            trigger = await session.get(
                TrainingEvaluationTriggerRow, job.evaluation_pause_trigger_id, with_for_update=True
            )
            if (
                trigger is not None
                and trigger.pause_version == job.version - 1
                and trigger.checkpoint_id == job.latest_checkpoint_id
                and trigger.fence == job.fence
            ):
                trigger.pause_version = job.version
                trigger.phase = "preparing_adapter"
        await append_event(
            session,
            job.id,
            TrainingStateEvent.model_validate(
                {
                    "kind": "terminal" if job.state in TERMINAL_TRAINING_STATES else "state",
                    "state": job.state,
                    "reason": job.safe_reason,
                }
            ),
        )
    return True


async def release_unstarted_job(session: AsyncSession, job_id: str) -> bool:
    job = await current_job(session, job_id, lock=True)
    active = await session.scalar(
        select(TrainingAttemptRow.id).where(
            TrainingAttemptRow.job_id == job.id,
            TrainingAttemptRow.state.in_(["preparing", "running", "stopping", "unknown"]),
        )
    )
    if active is not None:
        return False
    if job.state == "cancelling":
        job.state, job.finished_at = "cancelled", datetime.now(UTC)
        job.version += 1
        await append_event(
            session,
            job.id,
            TrainingStateEvent.model_validate(
                {"kind": "terminal", "state": job.state, "reason": job.safe_reason}
            ),
        )
        return True
    return False


@observed("coire.scheduler.training.restore_evictions")
async def restore_evictions(session: AsyncSession, attempt_id: str) -> None:
    """Offer idempotent exact-target reloads, preserving every newer admin mutation.

    This never resurrects a stopped instance or releases an uncertain victim.
    Requested replacement instances use the ordinary placement admission path.
    """
    import uuid

    from coire_api.auth import ADMIN
    from coire_api.db import NodeMemoryLedgerRow, TrainingAdapterRow
    from coire_api.gateway.targets import ModelNotFoundError, resolve_target
    from coire_api.instance.service import append_initial_transition
    from coire_api.placement.service import effective_occupied_bytes
    from coire_core.models.adapters import InferenceTarget
    from coire_core.models.instance import InstanceState
    from coire_scheduler.training_admission import restoration_version

    attempt = await session.get(TrainingAttemptRow, attempt_id)
    if attempt is None or attempt.state != "stopped":
        return
    intents = (
        await session.scalars(
            select(TrainingEvictionIntentRow).where(
                TrainingEvictionIntentRow.attempt_id == attempt_id,
                TrainingEvictionIntentRow.restoration_state == "pending",
            )
        )
    ).all()
    node_ids = list(
        (
            await session.scalars(
                select(MemoryReservationRow.node_id)
                .where(MemoryReservationRow.id.in_([intent.reservation_id for intent in intents]))
                .distinct()
            )
        ).all()
    )
    await lock_nodes_for_admission(session, node_ids)
    for intent in intents:
        locked_intent = await session.get(
            TrainingEvictionIntentRow, intent.id, populate_existing=True, with_for_update=True
        )
        assert locked_intent is not None
        intent = locked_intent
        if intent.restoration_state != "pending":
            continue
        hold = await session.get(
            MemoryReservationRow, intent.reservation_id, populate_existing=True
        )
        instance = await session.get(ModelInstanceRow, intent.instance_id, populate_existing=True)
        if (
            hold is None
            or instance is None
            or hold.state != MemoryReservationState.RELEASED
            or instance.state != InstanceState.STOPPED
        ):
            continue
        await lock_nodes_for_admission(session, [hold.node_id])
        hold = await session.get(
            MemoryReservationRow,
            intent.reservation_id,
            populate_existing=True,
            with_for_update=True,
        )
        instance = await session.get(
            ModelInstanceRow, intent.instance_id, populate_existing=True, with_for_update=True
        )
        assert hold is not None and instance is not None
        if (
            await restoration_version(session, instance, hold) != intent.prior_version
            or instance.policy != intent.prior_policy
            or hold.pinned
        ):
            intent.restoration_state = "superseded"
            continue
        model = await session.get(ModelRow, instance.model_id)
        if model is None or str(model.state) != "ready" or instance.variant_id is None:
            intent.restoration_state = "superseded"
            continue
        target = InferenceTarget.model_validate(intent.target)
        adapter = (
            await session.get(TrainingAdapterRow, target.adapter_id) if target.adapter_id else None
        )
        try:
            actual = await resolve_target(
                session, adapter.selector if adapter else target.model_id, ADMIN, target.variant_id
            )
        except (LookupError, ModelNotFoundError):
            intent.restoration_state = "superseded"
            continue
        if actual.identity != target:
            intent.restoration_state = "superseded"
            continue
        ledger = await session.get(
            NodeMemoryLedgerRow, hold.node_id, populate_existing=True, with_for_update=True
        )
        counted = (
            await session.scalars(
                select(MemoryReservationRow).where(
                    MemoryReservationRow.node_id == hold.node_id,
                    MemoryReservationRow.state.in_(
                        [
                            MemoryReservationState.PENDING,
                            MemoryReservationState.HELD,
                            MemoryReservationState.RELEASING,
                        ]
                    ),
                )
            )
        ).all()
        occupied = (
            effective_occupied_bytes(counted, ledger.measured_resident_bytes) if ledger else None
        )
        if ledger is None or occupied is None or occupied + hold.bytes > ledger.budget_bytes:
            continue
        replacement_id = uuid.uuid5(intent.id, "restore")
        if await session.get(ModelInstanceRow, replacement_id) is None:
            replacement = ModelInstanceRow(
                id=replacement_id,
                model_id=target.model_id,
                variant_id=target.variant_id,
                adapter_id=target.adapter_id,
                policy=intent.prior_policy,
                state=InstanceState.REQUESTED,
            )
            session.add(replacement)
            await session.flush()
            await append_initial_transition(session, replacement)
        intent.restoration_state = "offered"
