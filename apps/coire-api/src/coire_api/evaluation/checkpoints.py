"""Checkpoint ownership reducers; node I/O runs outside these transactions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    EngineProcessRow,
    EvaluationGroupRow,
    EvaluationRunRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    NodeRow,
    TrainingAdapterRow,
    TrainingArtifactCopyRow,
    TrainingAttemptRow,
    TrainingCheckpointRow,
    TrainingCommandRow,
    TrainingEvaluationTriggerRow,
    TrainingJobRow,
    TrainingParticipantRow,
    TrainingStorageReservationRow,
)
from coire_api.evaluation.authorization import authorize_live_evaluation_action
from coire_api.evaluation.telemetry import observed
from coire_core.errors import EvaluationForbidden, TrainingConflict
from coire_core.models.engine import EngineState
from coire_core.models.evaluation import TERMINAL_EVALUATION_STATES
from coire_core.models.instance import InstanceState
from coire_core.models.placement import MemoryReservationState
from coire_core.models.training_node import TrainingArtifactDeletionReceipt, TrainingStopReceipt
from coire_core.settings import Settings


async def require_checkpoint_run_owner(session: AsyncSession, run: EvaluationRunRow) -> None:
    """All checkpoint phases, including base/judge and qualification, share the pause fence."""
    group = await session.get(EvaluationGroupRow, run.group_id)
    if group is None or group.checkpoint_id is None or group.job_id is None:
        return
    if group.origin not in {"training_checkpoint", "measurement"}:
        return
    trigger = await session.scalar(
        select(TrainingEvaluationTriggerRow).where(
            TrainingEvaluationTriggerRow.job_id == group.job_id,
            TrainingEvaluationTriggerRow.checkpoint_id == group.checkpoint_id,
            TrainingEvaluationTriggerRow.boundary_kind == "checkpoint",
        )
    )
    if trigger is None:
        if group.origin == "measurement":
            return  # Completed-training measurement context has no checkpoint trigger.
        raise TrainingConflict("Checkpoint execution obligation is unavailable")
    job = await session.get(TrainingJobRow, group.job_id, populate_existing=True)
    if job is None or trigger.phase not in {"preparing_adapter", "evaluating"}:
        raise TrainingConflict("Checkpoint execution no longer owns its pause")
    from coire_api.evaluation.training import require_checkpoint_pause

    await require_checkpoint_pause(session, job, trigger.id)


@observed("coire.api.evaluation.checkpoint.cleanup")
async def finish_checkpoint_cleanup(
    session: AsyncSession, identity: uuid.UUID, *, settings: Settings
) -> bool:
    snapshot = await session.get(TrainingEvaluationTriggerRow, identity)
    if snapshot is None:
        return True
    parent = await session.get(TrainingJobRow, snapshot.job_id)
    if parent is None:
        return False
    revoked = False
    try:
        await authorize_live_evaluation_action(
            session, Principal.model_validate(parent.authorization_snapshot)
        )
    except EvaluationForbidden:
        revoked = True
    job = await session.get(TrainingJobRow, parent.id, with_for_update=True, populate_existing=True)
    trigger = await session.get(
        TrainingEvaluationTriggerRow, identity, with_for_update=True, populate_existing=True
    )
    assert job is not None and trigger is not None
    if trigger.phase == "complete":
        return True
    if trigger.phase not in {"cleaning_adapter", "resume_pending"} or trigger.group_id is None:
        return False
    runs = (
        await session.scalars(
            select(EvaluationRunRow).where(EvaluationRunRow.group_id == trigger.group_id)
        )
    ).all()
    if not runs or any(
        run.state not in TERMINAL_EVALUATION_STATES or run.cleanup_state != "complete"
        for run in runs
    ):
        return False
    checkpoint = await session.get(TrainingCheckpointRow, trigger.checkpoint_id)
    attempt = await session.get(TrainingAttemptRow, checkpoint.attempt_id) if checkpoint else None
    if attempt is None or attempt.state != "stopped" or attempt.stopped_at is None:
        return False
    participants = (
        await session.scalars(
            select(TrainingParticipantRow).where(TrainingParticipantRow.attempt_id == attempt.id)
        )
    ).all()
    if len(participants) != attempt.world_size or any(
        item.stopped_at is None or item.stop_proof is None for item in participants
    ):
        return False
    for participant in participants:
        proof = TrainingStopReceipt.model_validate(participant.stop_proof)
        hold = await session.get(MemoryReservationRow, participant.reservation_id)
        node = await session.get(NodeRow, participant.node_id)
        if (
            not proof.stopped
            or proof.attempt_id != attempt.id
            or proof.fence != trigger.fence
            or node is None
            or proof.node != node.name
            or proof.pid != participant.pid
            or proof.process_create_time != participant.process_create_time
            or hold is None
            or hold.holder_id != attempt.id
            or hold.node_id != participant.node_id
            or hold.state != MemoryReservationState.RELEASED
        ):
            return False
    adapter_id = uuid.uuid5(trigger.id, "checkpoint-adapter")
    adapter = await session.get(TrainingAdapterRow, adapter_id)
    extraction = await session.get(
        TrainingCommandRow, uuid.uuid5(trigger.id, "checkpoint-extraction")
    )
    if extraction is not None and extraction.state not in {"succeeded", "failed"}:
        return False
    if adapter is not None or (extraction is not None and extraction.state == "succeeded"):
        for node_name in ("coire-edge-a", "coire-edge-b"):
            command_id = uuid.uuid5(trigger.id, "checkpoint-adapter-delete:" + node_name)
            deletion = await session.get(TrainingCommandRow, command_id)
            if (
                deletion is None
                or deletion.operation != "evaluation.adapter.delete"
                or deletion.subject_id != node_name
                or deletion.job_id != job.id
                or deletion.state != "succeeded"
                or deletion.receipt is None
            ):
                return False
            proof_delete = TrainingArtifactDeletionReceipt.model_validate(deletion.receipt)
            if (
                proof_delete.command_id != command_id
                or proof_delete.artifact_id != adapter_id
                or not proof_delete.purged
            ):
                return False
    held_storage = await session.scalar(
        select(TrainingStorageReservationRow.id)
        .where(
            TrainingStorageReservationRow.subject_id == str(adapter_id),
            TrainingStorageReservationRow.state.in_(["held", "releasing"]),
        )
        .limit(1)
    )
    if held_storage is not None:
        return False
    if adapter is not None:
        if adapter.purpose != "evaluation" or adapter.evaluation_trigger_id != trigger.id:
            return False
        instances = (
            await session.scalars(
                select(ModelInstanceRow).where(ModelInstanceRow.adapter_id == adapter_id)
            )
        ).all()
        if any(
            item.state not in {InstanceState.STOPPED, InstanceState.FAILED} for item in instances
        ):
            return False
        unsafe_engine = await session.scalar(
            select(EngineProcessRow.id)
            .join(InstanceMemberRow, InstanceMemberRow.engine_id == EngineProcessRow.id)
            .join(ModelInstanceRow, ModelInstanceRow.id == InstanceMemberRow.instance_id)
            .where(
                ModelInstanceRow.adapter_id == adapter_id,
                EngineProcessRow.state != EngineState.STOPPED,
            )
            .limit(1)
        )
        unsafe_copy = await session.scalar(
            select(TrainingArtifactCopyRow.id)
            .where(
                TrainingArtifactCopyRow.adapter_id == adapter_id,
                TrainingArtifactCopyRow.state != "purged",
            )
            .limit(1)
        )
        if unsafe_engine is not None or unsafe_copy is not None or adapter.state != "retired":
            return False
    owned = (
        job.state == "paused"
        and job.pause_origin == "evaluation"
        and job.evaluation_pause_trigger_id == trigger.id
        and job.version == trigger.pause_version
        and job.fence == trigger.fence
        and job.latest_checkpoint_id == trigger.checkpoint_id
    )
    remaining = settings.training_execution_timeout_s - job.cumulative_execution_seconds
    resume_expired = datetime.now(UTC) >= trigger.deadline_at + timedelta(
        seconds=settings.evaluation_timeout_seconds + settings.training_queue_timeout_s
    )
    if owned and not revoked and settings.training_enabled and remaining > 0 and not resume_expired:
        trigger.phase, trigger.resume_disposition = "resume_pending", "pending"
        return False
    if job.evaluation_pause_trigger_id == trigger.id:
        job.evaluation_pause_trigger_id = None
        if job.pause_origin == "evaluation":
            job.pause_origin = "admin"
            job.safe_reason = "admin_pause"
            job.version += 1
            job.updated_at = datetime.now(UTC)
    trigger.phase, trigger.completed_at = "complete", datetime.now(UTC)
    trigger.resume_disposition = (
        "authorization_revoked"
        if revoked
        else "execution_timeout"
        if owned and remaining <= 0
        else "capacity_timeout"
        if owned and resume_expired
        else "admission_disabled"
        if owned
        else "operator_override"
    )
    from coire_api.audit import write_principal_audit

    await write_principal_audit(
        session,
        principal=Principal.model_validate(job.authorization_snapshot),
        action="evaluation.training.ownership.release",
        target_type="training_evaluation_trigger",
        target_id=str(trigger.id),
        context={"job_id": job.id, "resume_disposition": trigger.resume_disposition},
    )
    return True
