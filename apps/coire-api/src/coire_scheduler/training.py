"""Persisted prepare/start barriers and fenced command receipt reduction."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    MemoryReservationRow,
    NodeRow,
    TrainingAttemptRow,
    TrainingCommandRow,
    TrainingParticipantRow,
)
from coire_api.training.authorization import authorize_live_training_action
from coire_api.training.events import current_job
from coire_api.training.service import payload_digest, recheck_training_inputs
from coire_api.training.telemetry import observed
from coire_core.errors import TrainingConflict
from coire_core.models.placement import MemoryReservationState
from coire_core.models.training import ResolvedTrainingSpec
from coire_core.models.training_node import (
    CheckpointCommitAcknowledgement,
    NodeTrainingEvent,
    NodeTrainingStatus,
    TrainingLeaseRenewal,
    TrainingPauseRequest,
    TrainingPrepared,
    TrainingPrepareRequest,
    TrainingStartReceipt,
    TrainingStartRequest,
    TrainingStopRequest,
)


async def enqueue_attempt_commands(
    session: AsyncSession, attempt_id: str, phase: Literal["prepare", "start"]
) -> list[TrainingCommandRow]:
    attempt = await session.get(TrainingAttemptRow, attempt_id)
    if attempt is None:
        raise TrainingConflict("Training attempt is unavailable")
    job = await current_job(session, attempt.job_id)
    await authorize_live_training_action(
        session, Principal.model_validate(job.authorization_snapshot)
    )
    job = await current_job(session, attempt.job_id, lock=True)
    attempt = await session.get(
        TrainingAttemptRow, attempt_id, populate_existing=True, with_for_update=True
    )
    assert attempt is not None
    if (
        attempt.fence != job.fence
        or attempt.state != "preparing"
        or job.state != "reserving"
        or attempt.lease_expires_at <= datetime.now(UTC)
        or job.execution_deadline_at <= datetime.now(UTC)
    ):
        raise TrainingConflict("Attempt no longer has execution authority")
    resolved = ResolvedTrainingSpec.model_validate(job.resolved_spec)
    if payload_digest(resolved) != job.resolved_sha256:
        raise TrainingConflict("Resolved execution identity changed")
    await recheck_training_inputs(session, resolved)
    participants = list(
        (
            await session.scalars(
                select(TrainingParticipantRow)
                .where(TrainingParticipantRow.attempt_id == attempt.id)
                .order_by(TrainingParticipantRow.rank)
            )
        ).all()
    )
    if len(participants) != attempt.world_size:
        raise TrainingConflict("Attempt participant barrier is incomplete")
    from coire_scheduler.training_admission import victim_drain_barrier

    if not await victim_drain_barrier(session, attempt.id):
        return []
    prepared = list(
        (
            await session.scalars(
                select(TrainingCommandRow).where(
                    TrainingCommandRow.attempt_id == attempt.id,
                    TrainingCommandRow.operation == "node.training.prepare",
                )
            )
        ).all()
    )
    if phase == "start" and (
        len(prepared) != attempt.world_size
        or any(
            row.receipt is None or not TrainingPrepared.model_validate(row.receipt).ready
            for row in prepared
        )
    ):
        raise TrainingConflict("Every rank must prepare before any rank starts")
    commands = []
    for p in participants:
        node = await session.get(NodeRow, p.node_id)
        assert node is not None
        command_id = p.command_id if phase == "prepare" else uuid.uuid5(p.spawn_nonce, "start")
        prior = await session.get(TrainingCommandRow, command_id)
        if prior is not None:
            commands.append(prior)
            continue
        common = {
            "command_id": command_id,
            "job_id": job.id,
            "attempt_id": attempt.id,
            "fence": attempt.fence,
            "request_sha256": p.request_sha256,
            "node": node.name,
            "rank": p.rank,
            "world_size": attempt.world_size,
            "lease_expires_at": attempt.lease_expires_at,
        }
        request: TrainingPrepareRequest | TrainingStartRequest
        if phase == "prepare":
            if p.disk_reservation_id is None:
                raise TrainingConflict("Participant disk hold is missing")
            from coire_api.db import TrainingCheckpointRow

            checkpoint = (
                await session.get(TrainingCheckpointRow, attempt.resume_checkpoint_id)
                if attempt.resume_checkpoint_id
                else None
            )
            request = TrainingPrepareRequest.model_validate(
                {
                    **common,
                    "resolved": resolved,
                    "reservation_id": p.reservation_id,
                    "disk_reservation_id": p.disk_reservation_id,
                    "resume_checkpoint_id": checkpoint.id if checkpoint else None,
                    "resume_manifest_sha256": checkpoint.manifest_sha256 if checkpoint else None,
                }
            )
        else:
            request = TrainingStartRequest.model_validate(
                {**common, "prepared_command_id": p.command_id, "spawn_nonce": p.spawn_nonce}
            )
        row = TrainingCommandRow(
            id=command_id,
            actor_user_id=job.owner_user_id,
            idempotency_key=f"{attempt.id}:{phase}:{p.rank}",
            operation=f"node.training.{phase}",
            subject_id=node.name,
            job_id=job.id,
            attempt_id=attempt.id,
            request_sha256=payload_digest(request),
            payload=request.model_dump(mode="json"),
            state="pending",
        )
        session.add(row)
        commands.append(row)
    await session.flush()
    return commands


async def reduce_command_receipt(
    session: AsyncSession, command_id: uuid.UUID, receipt: TrainingPrepared | TrainingStartReceipt
) -> None:
    row = await session.get(TrainingCommandRow, command_id)
    if row is None or row.job_id is None or row.attempt_id is None:
        raise TrainingConflict("Unknown runtime command")
    job = await current_job(session, row.job_id, lock=True)
    row = await session.get(
        TrainingCommandRow, command_id, populate_existing=True, with_for_update=True
    )
    assert row is not None
    attempt = await session.get(
        TrainingAttemptRow, row.attempt_id, populate_existing=True, with_for_update=True
    )
    if (
        attempt is None
        or attempt.fence != job.fence
        or receipt.fence != attempt.fence
        or receipt.attempt_id != attempt.id
        or attempt.state not in {"preparing", "running", "stopping", "unknown"}
    ):
        raise TrainingConflict("Stale runtime command receipt")
    p = await session.scalar(
        select(TrainingParticipantRow)
        .where(
            TrainingParticipantRow.attempt_id == attempt.id,
            TrainingParticipantRow.reservation_id == receipt.reservation_id,
        )
        .with_for_update()
    )
    if p is None:
        raise TrainingConflict("Runtime receipt reservation differs")
    if isinstance(receipt, TrainingPrepared):
        if (
            row.operation != "node.training.prepare"
            or receipt.node != row.subject_id
            or receipt.runtime_sha256 != attempt.runtime_sha256
        ):
            raise TrainingConflict("Prepared runtime identity differs")
        if (
            not receipt.ready
            and receipt.reason == "checkpoint_invalid"
            and attempt.resume_checkpoint_id is not None
        ):
            from coire_api.db import TrainingCheckpointRow

            checkpoint = await session.get(
                TrainingCheckpointRow,
                attempt.resume_checkpoint_id,
                populate_existing=True,
                with_for_update=True,
            )
            if checkpoint is not None:
                checkpoint.state = "corrupt"
    else:
        if row.operation != "node.training.start":
            raise TrainingConflict("Start receipt does not belong to a start command")
    payload = receipt.model_dump(mode="json")
    if row.receipt is not None:
        if row.receipt != payload:
            raise TrainingConflict("Runtime receipt is immutable")
        return
    row.receipt, row.state = payload, "succeeded"
    if isinstance(receipt, TrainingStartReceipt):
        p.pid, p.process_create_time = receipt.pid, receipt.process_create_time
        hold = await session.get(MemoryReservationRow, p.reservation_id)
        assert hold is not None
        hold.state = MemoryReservationState.HELD
        await session.flush()
        missing = await session.scalar(
            select(TrainingParticipantRow.id).where(
                TrainingParticipantRow.attempt_id == attempt.id,
                TrainingParticipantRow.pid.is_(None),
            )
        )
        if (
            missing is None
            and job.state == "reserving"
            and attempt.lease_expires_at > datetime.now(UTC)
        ):
            attempt.state, job.state = "running", "running"
            job.version += 1
            from datetime import timedelta

            from coire_core.settings import get_settings

            remaining = (
                get_settings().training_execution_timeout_s - job.cumulative_execution_seconds
            )
            job.execution_deadline_at = datetime.now(UTC) + timedelta(seconds=max(0, remaining))
            job.updated_at = datetime.now(UTC)
        elif job.state not in {"reserving", "running"} or attempt.lease_expires_at <= datetime.now(
            UTC
        ):
            attempt.state = "stopping"
    await session.flush()


async def enqueue_controls(
    session: AsyncSession, attempt_id: str, operation: Literal["pause", "stop", "lease"]
) -> list[TrainingCommandRow]:
    from datetime import timedelta

    attempt = await session.get(TrainingAttemptRow, attempt_id)
    if attempt is None:
        raise TrainingConflict("Unknown attempt")
    job = await current_job(session, attempt.job_id)
    if operation == "lease":
        await authorize_live_training_action(
            session, Principal.model_validate(job.authorization_snapshot)
        )
    job = await current_job(session, attempt.job_id, lock=True)
    attempt = await session.get(
        TrainingAttemptRow, attempt_id, populate_existing=True, with_for_update=True
    )
    assert attempt is not None
    if attempt.state == "stopped":
        return []
    now = datetime.now(UTC)
    if operation == "lease":
        await authorize_live_training_action(
            session, Principal.model_validate(job.authorization_snapshot)
        )
        await recheck_training_inputs(
            session, ResolvedTrainingSpec.model_validate(job.resolved_spec)
        )
        if job.state not in {"running", "reserving", "pausing"} or job.execution_deadline_at <= now:
            raise TrainingConflict("Execution authority cannot be renewed")
        expiry = min(now + timedelta(seconds=30), job.execution_deadline_at)
    else:
        # Stop does not depend on revoked execution authority or a live lease.
        expiry = min(now + timedelta(seconds=30), job.execution_deadline_at)
    participants = list(
        (
            await session.scalars(
                select(TrainingParticipantRow).where(
                    TrainingParticipantRow.attempt_id == attempt.id,
                    TrainingParticipantRow.stopped_at.is_(None),
                )
            )
        ).all()
    )
    rows = []
    for p in participants:
        node = await session.get(NodeRow, p.node_id)
        assert node is not None
        identity = (
            f"{operation}:{job.version}:{int(now.timestamp()) // 10 if operation == 'lease' else 0}"
        )
        command_id = uuid.uuid5(p.spawn_nonce, identity)
        prior = await session.get(TrainingCommandRow, command_id)
        if prior is not None:
            rows.append(prior)
            continue
        common = {
            "command_id": command_id,
            "job_id": job.id,
            "attempt_id": attempt.id,
            "fence": attempt.fence,
            "request_sha256": p.request_sha256,
            "node": node.name,
            "rank": p.rank,
            "world_size": attempt.world_size,
            "lease_expires_at": expiry,
        }
        request: TrainingPauseRequest | TrainingStopRequest | TrainingLeaseRenewal
        if operation == "pause":
            request = TrainingPauseRequest.model_validate(
                {**common, "reason": job.safe_reason or "admin_pause"}
            )
        elif operation == "stop":
            request = TrainingStopRequest.model_validate(
                {**common, "reason": job.safe_reason or "rank_failed"}
            )
        else:
            request = TrainingLeaseRenewal.model_validate(common)
        row = TrainingCommandRow(
            id=command_id,
            actor_user_id=job.owner_user_id,
            idempotency_key=f"{attempt.id}:{p.rank}:{identity}",
            operation=f"node.training.{operation}",
            subject_id=node.name,
            job_id=job.id,
            attempt_id=attempt.id,
            request_sha256=payload_digest(request),
            payload=request.model_dump(mode="json"),
            state="pending",
        )
        session.add(row)
        rows.append(row)
    if operation == "lease":
        attempt.lease_expires_at = expiry
    elif operation == "stop":
        attempt.state = "stopping"
    await session.flush()
    return rows


async def observe_training(session: AsyncSession, status: NodeTrainingStatus) -> None:
    from coire_core.models.training_node import TrainingStopReceipt
    from coire_scheduler.training_recovery import record_stop_proof

    job = await current_job(session, status.job_id, lock=True)
    attempt = await session.get(
        TrainingAttemptRow, status.attempt_id, populate_existing=True, with_for_update=True
    )
    if (
        attempt is None
        or attempt.job_id != job.id
        or attempt.fence != status.fence
        or attempt.fence != job.fence
    ):
        raise TrainingConflict("Observation belongs to another attempt")
    node = await session.scalar(select(NodeRow).where(NodeRow.name == status.node))
    if node is None:
        raise TrainingConflict("Observation belongs to an undeclared node")
    p = await session.scalar(
        select(TrainingParticipantRow)
        .where(
            TrainingParticipantRow.attempt_id == attempt.id,
            TrainingParticipantRow.node_id == node.id,
        )
        .with_for_update()
    )
    if p is None:
        raise TrainingConflict("Observation belongs to another participant")
    if attempt.state == "stopped" and status.liveness != "stopped":
        raise TrainingConflict("A stopped attempt cannot be re-adopted by a stale status")
    if status.pid is not None:
        if p.pid is not None and (
            p.pid != status.pid or p.process_create_time != status.process_create_time
        ):
            raise TrainingConflict("Observed process differs from owned spawn")
        p.pid, p.process_create_time = status.pid, status.process_create_time
    p.footprint_bytes = status.footprint_bytes
    if status.liveness == "stopped":
        await record_stop_proof(
            session,
            attempt.id,
            TrainingStopReceipt(
                attempt_id=attempt.id,
                fence=attempt.fence,
                node=status.node,
                pid=status.pid,
                process_create_time=status.process_create_time,
                stopped=True,
                observed_at=datetime.now(UTC),
            ),
        )
    elif status.liveness in {"unknown", "orphan"}:
        attempt.state = "unknown"
        if job.state not in {"cancelling", "pausing", "succeeded", "failed", "cancelled", "paused"}:
            job.state, job.safe_reason = "recovering", "node_unreachable"
    elif status.liveness == "running" and job.state in {"cancelling", "recovering"}:
        await enqueue_controls(session, attempt.id, "stop")


@observed("coire.scheduler.training.event.ingest")
async def ingest_training_event(
    session: AsyncSession, node_name: str, event: NodeTrainingEvent
) -> None:
    """Consume authenticated node mailboxes exactly once, without treating events as death proofs."""
    from coire_api.training.checkpoints import stage_checkpoint
    from coire_api.training.events import current_attempt, record_metric

    job = await current_job(session, event.job_id, lock=True)
    node = await session.scalar(select(NodeRow).where(NodeRow.name == node_name))
    participant = (
        await session.scalar(
            select(TrainingParticipantRow).where(
                TrainingParticipantRow.attempt_id == event.attempt_id,
                TrainingParticipantRow.node_id == node.id,
            )
        )
        if node is not None
        else None
    )
    if node is None or participant is None:
        raise TrainingConflict("Event belongs to a different participant")
    identity = uuid.uuid5(node.id, f"training-event:{event.attempt_id}:{event.sequence}")
    existing = await session.get(TrainingCommandRow, identity)
    if existing is not None:
        if existing.payload != event.model_dump(mode="json"):
            raise TrainingConflict("Node event sequence is immutable")
        return
    if event.payload.kind in {"failure", "stopped"}:
        attempt = await session.get(TrainingAttemptRow, event.attempt_id)
        if (
            attempt is None
            or attempt.job_id != job.id
            or attempt.fence != event.fence
            or job.fence != event.fence
        ):
            raise TrainingConflict("Control event is fenced")
    else:
        await current_attempt(session, job, event.attempt_id, event.fence)
    previous = await session.scalar(
        select(
            func.coalesce(func.max(TrainingCommandRow.payload["sequence"].as_integer()), 0)
        ).where(
            TrainingCommandRow.operation == "node.training.event",
            TrainingCommandRow.attempt_id == event.attempt_id,
            TrainingCommandRow.subject_id == node_name,
        )
    )
    if event.sequence != int(previous or 0) + 1:
        raise TrainingConflict("Node event sequence is not contiguous")
    if event.payload.kind == "progress":
        metric = event.payload.metric
        if (
            metric.update != event.update
            or metric.job_id != job.id
            or metric.attempt_id != event.attempt_id
        ):
            raise TrainingConflict("Worker metric differs from the event identity")
        # Bare training reports on every rank. Keep each authenticated mailbox
        # receipt, but publish one job-level loss row from the owned rank zero:
        # timing, footprint and recorded-at naturally differ between ranks.
        if participant.rank == 0:
            await record_metric(session, metric, fence=event.fence)
    elif event.payload.kind == "checkpoint_rank_staged":
        from coire_scheduler.training_components import record_rank_component

        await record_rank_component(session, node_name, event)
    elif event.payload.kind == "checkpoint_staged":
        manifest = event.payload.manifest
        if (
            manifest.job_id != job.id
            or manifest.attempt_id != event.attempt_id
            or manifest.fence != event.fence
            or manifest.update != event.update
        ):
            raise TrainingConflict("Worker checkpoint differs from the event identity")
        await stage_checkpoint(session, manifest)
    elif event.payload.kind == "pause_requested":
        if event.payload.reason not in {
            "admin_pause",
            "latency_breach",
            "thermal_breach",
            "memory_breach",
            "lease_expired",
        }:
            raise TrainingConflict("Worker pause reason is unsupported")
        if job.state == "running":
            job.state, job.pause_origin, job.safe_reason = (
                "pausing",
                "protective",
                event.payload.reason,
            )
            job.version += 1
            job.updated_at = datetime.now(UTC)
    elif event.payload.kind == "failure":
        if job.state not in {"cancelling", "pausing", "succeeded", "failed", "cancelled", "paused"}:
            job.state, job.safe_reason = "recovering", event.payload.reason
            job.version += 1
            job.updated_at = datetime.now(UTC)
    # 'stopped' is diagnostic intent. Only the authenticated status/stop lane
    # can provide process-identity termination proof and release reservations.
    session.add(
        TrainingCommandRow(
            id=identity,
            actor_user_id=job.owner_user_id,
            idempotency_key=f"node-event:{event.attempt_id}:{node_name}:{event.sequence}",
            operation="node.training.event",
            subject_id=node_name,
            job_id=job.id,
            attempt_id=event.attempt_id,
            request_sha256=payload_digest(event),
            payload=event.model_dump(mode="json"),
            receipt=event.model_dump(mode="json"),
            state="succeeded",
        )
    )
    await session.flush()


async def enqueue_checkpoint_acknowledgements(
    session: AsyncSession, checkpoint_id: uuid.UUID
) -> list[TrainingCommandRow]:
    from coire_api.training.checkpoints import commit_checkpoint

    checkpoint = await commit_checkpoint(session, checkpoint_id)
    job = await current_job(session, checkpoint.job_id, lock=True)
    attempt = await session.get(TrainingAttemptRow, checkpoint.attempt_id)
    assert attempt is not None
    if checkpoint.fence != job.fence or attempt.lease_expires_at <= datetime.now(UTC):
        raise TrainingConflict("Checkpoint acknowledgement is fenced or expired")
    participants = list(
        (
            await session.scalars(
                select(TrainingParticipantRow).where(
                    TrainingParticipantRow.attempt_id == attempt.id
                )
            )
        ).all()
    )
    rows = []
    for participant in participants:
        node = await session.get(NodeRow, participant.node_id)
        assert node is not None
        identity = uuid.uuid5(participant.spawn_nonce, f"checkpoint-commit:{checkpoint_id}")
        prior = await session.get(TrainingCommandRow, identity)
        if prior is not None:
            rows.append(prior)
            continue
        request = CheckpointCommitAcknowledgement.model_validate(
            {
                "command_id": identity,
                "job_id": job.id,
                "attempt_id": attempt.id,
                "fence": attempt.fence,
                "request_sha256": participant.request_sha256,
                "node": node.name,
                "rank": participant.rank,
                "world_size": attempt.world_size,
                "lease_expires_at": attempt.lease_expires_at,
                "checkpoint_id": checkpoint.id,
                "manifest_sha256": checkpoint.manifest_sha256,
                "update": checkpoint.completed_update,
            }
        )
        row = TrainingCommandRow(
            id=identity,
            actor_user_id=job.owner_user_id,
            idempotency_key=f"checkpoint-commit:{checkpoint_id}:{participant.rank}",
            operation="node.training.checkpoint-commit",
            subject_id=node.name,
            job_id=job.id,
            attempt_id=attempt.id,
            request_sha256=payload_digest(request),
            payload=request.model_dump(mode="json"),
            state="pending",
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def advance_training(session: AsyncSession, job_id: str) -> None:
    """One durable scheduler tick. API only dispatches committed node commands."""
    from coire_api.training.checkpoints import recovery_checkpoint
    from coire_core.errors import TrainingForbidden
    from coire_scheduler.training_admission import admit_training
    from coire_scheduler.training_recovery import release_unstarted_job

    job = await current_job(session, job_id)
    authorized = True
    try:
        await authorize_live_training_action(
            session, Principal.model_validate(job.authorization_snapshot)
        )
    except TrainingForbidden:
        authorized = False
    job = await current_job(session, job_id, lock=True)
    now = datetime.now(UTC)
    active = await session.scalar(
        select(TrainingAttemptRow).where(
            TrainingAttemptRow.job_id == job.id,
            TrainingAttemptRow.state.in_(["preparing", "running", "stopping", "unknown"]),
        )
    )
    if job.state in {"succeeded", "failed", "cancelled", "paused"}:
        return
    if (
        active is None
        and job.state in {"queued", "preflighting", "recovering"}
        and job.queue_deadline_at <= now
    ):
        job.state, job.safe_reason, job.finished_at = "failed", "queue_timeout", now
        job.version += 1
        job.updated_at = now
        return
    if not authorized:
        job.state, job.safe_reason = "cancelling", "unauthorized"
    if job.execution_deadline_at <= now and (active is not None or job.state == "finalizing"):
        job.state, job.safe_reason = "cancelling", "execution_timeout"
    if (
        active is not None
        and active.lease_expires_at <= now
        and job.state in {"running", "reserving"}
    ):
        job.state, job.safe_reason = "recovering", "lease_expired"
    if job.state == "cancelling":
        if active is not None:
            await enqueue_controls(session, active.id, "stop")
        else:
            await release_unstarted_job(session, job.id)
        return
    if job.state == "pausing" and active is not None:
        await enqueue_controls(
            session, active.id, "stop" if (now - job.updated_at).total_seconds() >= 60 else "pause"
        )
        return
    if job.state == "recovering" and active is not None:
        await enqueue_controls(session, active.id, "stop")
        return
    if job.state == "queued" and active is None:
        from coire_api.training.events import append_event
        from coire_core.models.training import TrainingStateEvent

        job.state = "preflighting"
        job.version += 1
        job.updated_at = now
        await append_event(
            session,
            job.id,
            TrainingStateEvent.model_validate({"kind": "state", "state": "preflighting"}),
        )
        return
    if job.state in {"running", "recovering"} and active is None and job.latest_checkpoint_id:
        from coire_api.db import TrainingCheckpointRow

        checkpoint = await session.get(TrainingCheckpointRow, job.latest_checkpoint_id)
        resolved = ResolvedTrainingSpec.model_validate(job.resolved_spec)
        if (
            checkpoint is not None
            and checkpoint.state == "committed"
            and checkpoint.fence == job.fence
            and checkpoint.completed_update == resolved.spec.optim.updates
        ):
            await recheck_training_inputs(session, resolved)
            job.state, job.safe_reason = "finalizing", None
            job.version += 1
            job.updated_at = now
            from coire_api.training.events import append_event
            from coire_core.models.training import TrainingStateEvent

            await append_event(
                session,
                job.id,
                TrainingStateEvent.model_validate({"kind": "state", "state": "finalizing"}),
            )
            return
    if job.state == "preflighting" and active is None:
        job.state = "queued"
    if job.state in {"queued", "recovering"} and active is None:
        recovering = job.state == "recovering"
        if job.state == "recovering":
            if job.recovery_attempts >= 3:
                job.state, job.safe_reason = "failed", "recovery_exhausted"
                job.finished_at = now
                return
            checkpoint = await recovery_checkpoint(session, job)
            if job.latest_checkpoint_id is not None and checkpoint is None:
                job.state, job.safe_reason = "failed", "checkpoint_invalid"
                job.finished_at = now
                return
            job.latest_checkpoint_id = checkpoint.id if checkpoint else None
            job.completed_update = checkpoint.completed_update if checkpoint else 0
            from sqlalchemy import update

            from coire_api.db import TrainingMetricRow
            from coire_api.training.events import append_event
            from coire_core.models.training import TrainingRecoveryEvent

            await session.execute(
                update(TrainingMetricRow)
                .where(
                    TrainingMetricRow.job_id == job.id,
                    TrainingMetricRow.completed_update > job.completed_update,
                )
                .values(rolled_back=True)
            )
            selection_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"coire:recovery:{job.id}:{job.fence}:{job.latest_checkpoint_id}",
            )
            if await session.get(TrainingCommandRow, selection_id) is None:
                recovery = TrainingRecoveryEvent.model_validate(
                    {
                        "reason": job.safe_reason or "rank_failed",
                        "resume_update": job.completed_update,
                    }
                )
                await append_event(session, job.id, recovery)
                session.add(
                    TrainingCommandRow(
                        id=selection_id,
                        actor_user_id=job.owner_user_id,
                        idempotency_key=f"recovery-selection:{job.id}:{job.fence}:{job.latest_checkpoint_id}",
                        operation="training.recovery.selection",
                        subject_id=job.id,
                        job_id=job.id,
                        request_sha256=payload_digest(recovery),
                        payload=recovery.model_dump(mode="json"),
                        receipt=recovery.model_dump(mode="json"),
                        state="succeeded",
                    )
                )
        resolved = ResolvedTrainingSpec.model_validate(job.resolved_spec)
        names = (
            [resolved.spec.placement.preferred_node]
            if resolved.spec.placement.preferred_node
            else ["coire-edge-a", "coire-edge-b"]
        )
        nodes = list(
            (
                await session.scalars(
                    select(NodeRow).where(NodeRow.name.in_(names)).order_by(NodeRow.name)
                )
            ).all()
        )
        if resolved.spec.placement.mode == "single":
            for node in nodes:
                active = await admit_training(session, job, [node])
                if active is not None or job.state == "failed":
                    break
        else:
            active = await admit_training(session, job, nodes)
        if active is not None:
            if recovering:
                job.recovery_attempts += 1
            await enqueue_attempt_commands(session, active.id, "prepare")
        return
    if job.state == "reserving" and active is not None:
        if active.lease_expires_at <= now:
            job.state, job.safe_reason = "recovering", "lease_expired"
            await enqueue_controls(session, active.id, "stop")
            return
        prepared = list(
            (
                await session.scalars(
                    select(TrainingCommandRow).where(
                        TrainingCommandRow.attempt_id == active.id,
                        TrainingCommandRow.operation == "node.training.prepare",
                    )
                )
            ).all()
        )
        if not prepared:
            await enqueue_attempt_commands(session, active.id, "prepare")
        if len(prepared) == active.world_size and all(p.receipt is not None for p in prepared):
            if all(TrainingPrepared.model_validate(p.receipt).ready for p in prepared):
                await enqueue_attempt_commands(session, active.id, "start")
            else:
                job.state, job.safe_reason = "recovering", "invalid_input"
                await enqueue_controls(session, active.id, "stop")
    if (
        job.state in {"running", "reserving"}
        and active is not None
        and (active.lease_expires_at - now).total_seconds() <= 20
    ):
        await enqueue_controls(session, active.id, "lease")
