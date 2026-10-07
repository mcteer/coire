"""Bounded metadata retirement preserves live references and unresolved cleanup."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    NodeRow,
    TrainingAdapterRow,
    TrainingArtifactCopyRow,
    TrainingAttemptRow,
    TrainingCheckpointRow,
    TrainingCommandRow,
    TrainingEventRow,
    TrainingJobRow,
    TrainingParticipantRow,
    TrainingStorageReservationRow,
)
from coire_api.training.service import begin_command, record_receipt, require_job_version
from coire_core.errors import TrainingConflict
from coire_core.models.training import (
    TERMINAL_TRAINING_STATES,
    TrainingDeleteRequest,
    TrainingDeletionReceipt,
)
from coire_core.models.training_node import (
    TrainingArtifactDeleteRequest,
    TrainingArtifactDeletionReceipt,
    TrainingArtifactManifest,
    TrainingAttemptCleanupReceipt,
    TrainingAttemptCleanupRequest,
)


async def retire_job(
    session: AsyncSession,
    principal: Principal,
    job_id: str,
    request: TrainingDeleteRequest,
    key: str,
) -> TrainingDeletionReceipt:
    command = await begin_command(
        session,
        principal,
        operation="training.delete",
        subject_id=job_id,
        idempotency_key=key,
        request=request,
    )
    if command.receipt is not None:
        return TrainingDeletionReceipt.model_validate(command.receipt)
    job = await require_job_version(session, job_id, request.expected_version)
    if job.state not in TERMINAL_TRAINING_STATES:
        raise TrainingConflict("Only terminal training jobs may be retired")
    active = await session.scalar(
        select(TrainingAttemptRow.id).where(
            TrainingAttemptRow.job_id == job.id,
            TrainingAttemptRow.state.in_(["preparing", "running", "stopping", "unknown"]),
        )
    )
    if active is not None:
        raise TrainingConflict("Trainer termination remains unresolved")
    if (
        await session.scalar(
            select(TrainingAdapterRow.id)
            .where(
                TrainingAdapterRow.source_job_id == job.id,
                TrainingAdapterRow.state.in_(["validating", "replicating"]),
            )
            .limit(1)
        )
        is not None
    ):
        raise TrainingConflict("Adapter extraction or replication still references this job")
    # Do not physically delete lineage rows: adapters and completed audits retain them.
    job.deleted_at, job.version = datetime.now(UTC), job.version + 1
    receipt = TrainingDeletionReceipt(job_id=job.id, state="retired")
    await record_receipt(session, principal, command, receipt)
    return receipt


async def retention_candidates(
    session: AsyncSession, job_id: str, *, keep: int = 3, byte_limit: int = 20 * 1024**3
) -> list[TrainingCheckpointRow]:
    if not 1 <= keep <= 3 or byte_limit < 1:
        raise ValueError("invalid checkpoint retention bounds")
    rows = list(
        (
            await session.scalars(
                select(TrainingCheckpointRow)
                .where(
                    TrainingCheckpointRow.job_id == job_id,
                    TrainingCheckpointRow.state == "committed",
                )
                .order_by(
                    TrainingCheckpointRow.completed_update.desc(),
                    TrainingCheckpointRow.created_at.desc(),
                )
            )
        ).all()
    )
    pinned = set(
        (
            await session.scalars(
                select(TrainingAttemptRow.resume_checkpoint_id).where(
                    TrainingAttemptRow.job_id == job_id,
                    TrainingAttemptRow.state.in_(["preparing", "running", "stopping", "unknown"]),
                )
            )
        ).all()
    )
    job = await session.get(TrainingJobRow, job_id)
    if job is not None and job.latest_checkpoint_id is not None:
        pinned.add(job.latest_checkpoint_id)
    pinned.update(
        (
            await session.scalars(
                select(TrainingAdapterRow.source_checkpoint_id).where(
                    TrainingAdapterRow.source_job_id == job_id,
                    TrainingAdapterRow.state.in_(["validating", "replicating"]),
                )
            )
        ).all()
    )
    retained_bytes = 0
    candidates = []
    for index, row in enumerate(rows):
        if (
            index == 0
            or row.id in pinned
            or (index < keep and retained_bytes + row.total_bytes <= byte_limit)
        ):
            retained_bytes += row.total_bytes
        else:
            candidates.append(row)
    return candidates


async def record_deletion(
    session: AsyncSession, copy_id: uuid.UUID, receipt: TrainingArtifactDeletionReceipt
) -> None:
    candidate = await session.get(TrainingArtifactCopyRow, copy_id)
    if candidate is None:
        raise TrainingConflict("Unknown artifact copy")
    if candidate.checkpoint_id is not None:
        checkpoint = await session.get(TrainingCheckpointRow, candidate.checkpoint_id)
        if checkpoint is None:
            raise TrainingConflict("Unknown checkpoint")
        from coire_api.db import TrainingJobRow

        await session.get(
            TrainingJobRow, checkpoint.job_id, populate_existing=True, with_for_update=True
        )
        await session.get(
            TrainingCheckpointRow, checkpoint.id, populate_existing=True, with_for_update=True
        )
    else:
        adapter = await session.get(TrainingAdapterRow, candidate.adapter_id)
        if adapter is None:
            raise TrainingConflict("Unknown adapter")
        from coire_api.db import TrainingJobRow

        await session.get(
            TrainingJobRow, adapter.source_job_id, populate_existing=True, with_for_update=True
        )
        await session.get(
            TrainingAdapterRow, adapter.id, populate_existing=True, with_for_update=True
        )
    row = await session.get(
        TrainingArtifactCopyRow, copy_id, populate_existing=True, with_for_update=True
    )
    if row is None or row.artifact_id != receipt.artifact_id or not receipt.purged:
        raise TrainingConflict("Artifact deletion remains unproved")
    if row.state != "deleting":
        raise TrainingConflict("No authorized cleanup intent for this artifact copy")
    command = await session.get(TrainingCommandRow, receipt.command_id, with_for_update=True)
    if (
        command is None
        or command.operation != "node.artifact.delete"
        or (command.subject_id != str(copy_id))
    ):
        raise TrainingConflict("Artifact cleanup receipt has no matching command")
    request = TrainingArtifactDeleteRequest.model_validate(command.payload)
    if request.manifest_sha256 != row.manifest_sha256:
        raise TrainingConflict("Artifact cleanup manifest differs from its command")
    row.cleanup_receipt, row.state = receipt.model_dump(mode="json"), "purged"
    command.receipt, command.state = receipt.model_dump(mode="json"), "succeeded"
    await session.flush()
    if row.checkpoint_id is not None:
        remaining = await session.scalar(
            select(TrainingArtifactCopyRow.id)
            .where(
                TrainingArtifactCopyRow.checkpoint_id == row.checkpoint_id,
                TrainingArtifactCopyRow.state != "purged",
            )
            .limit(1)
        )
        if remaining is None:
            checkpoint = await session.get(TrainingCheckpointRow, row.checkpoint_id)
            assert checkpoint is not None
            checkpoint.state, checkpoint.purged_at = "purged", datetime.now(UTC)
            job = await session.get(TrainingJobRow, checkpoint.job_id)
            assert job is not None
            if job.latest_checkpoint_id == checkpoint.id:
                if job.deleted_at is None:
                    raise TrainingConflict("Latest live recovery point cannot be purged")
                job.latest_checkpoint_id = None


async def enqueue_checkpoint_cleanup(session: AsyncSession, job_id: str) -> list[uuid.UUID]:
    """Fence both-copy retirement with the same job lock used by resume/promotion."""
    from coire_api.training.service import payload_digest
    from coire_core.models.training import ResolvedTrainingSpec

    job = await session.get(TrainingJobRow, job_id, populate_existing=True, with_for_update=True)
    if job is None:
        raise TrainingConflict("Cleanup job is unavailable")
    if job.resolved_spec is None:
        return []
    resolved = ResolvedTrainingSpec.model_validate(job.resolved_spec)
    candidates = await retention_candidates(
        session,
        job.id,
        keep=resolved.spec.output.keep_last_checkpoints,
    )
    if job.deleted_at is not None:
        if (
            job.state not in TERMINAL_TRAINING_STATES
            or await session.scalar(
                select(TrainingAttemptRow.id)
                .where(
                    TrainingAttemptRow.job_id == job.id,
                    TrainingAttemptRow.state.in_(["preparing", "running", "stopping", "unknown"]),
                )
                .limit(1)
            )
            is not None
        ):
            raise TrainingConflict("Retired job still has unresolved execution")
        if (
            await session.scalar(
                select(TrainingAdapterRow.id)
                .where(
                    TrainingAdapterRow.source_job_id == job.id,
                    TrainingAdapterRow.state.in_(["validating", "replicating"]),
                )
                .limit(1)
            )
            is not None
        ):
            return []
        candidates = list(
            await session.scalars(
                select(TrainingCheckpointRow).where(
                    TrainingCheckpointRow.job_id == job.id,
                    TrainingCheckpointRow.state.in_(
                        ["committed", "corrupt", "staging", "replicating"]
                    ),
                )
            )
        )
    identities: list[uuid.UUID] = []
    for checkpoint in candidates[:8]:
        copies = list(
            await session.scalars(
                select(TrainingArtifactCopyRow)
                .where(
                    TrainingArtifactCopyRow.checkpoint_id == checkpoint.id,
                    TrainingArtifactCopyRow.state.in_(
                        ["verified", "deleting", "pending", "failed"]
                        if job.deleted_at is not None
                        else ["verified", "deleting"]
                    ),
                )
                .with_for_update()
            )
        )
        for copy in copies:
            command_id = uuid.uuid5(copy.id, "artifact-delete")
            command = await session.get(TrainingCommandRow, command_id)
            request = TrainingArtifactDeleteRequest(
                command_id=command_id,
                manifest_sha256=copy.manifest_sha256,
                expected_version=1,
                unreferenced=True,
                expected_manifest=TrainingArtifactManifest.model_validate(checkpoint.manifest),
            )
            if command is None:
                session.add(
                    TrainingCommandRow(
                        id=command_id,
                        actor_user_id=job.owner_user_id,
                        operation="node.artifact.delete",
                        subject_id=str(copy.id),
                        job_id=job.id,
                        attempt_id=checkpoint.attempt_id,
                        idempotency_key=f"artifact-delete:{copy.id}",
                        request_sha256=payload_digest(request),
                        payload=request.model_dump(mode="json"),
                        state="pending",
                    )
                )
            elif command.payload != request.model_dump(mode="json"):
                raise TrainingConflict("Immutable artifact cleanup intent changed")
            copy.state = "deleting"
            identities.append(command_id)
        if copies:
            checkpoint.state = "purging"
    return identities


async def expire_events(session: AsyncSession) -> int:
    result = await session.execute(
        delete(TrainingEventRow).where(
            TrainingEventRow.occurred_at < datetime.now(UTC) - timedelta(days=7)
        )
    )
    return int(getattr(result, "rowcount", 0))


async def enqueue_attempt_cleanup(session: AsyncSession, job_id: str) -> list[uuid.UUID]:
    from coire_api.training.service import payload_digest

    job = await session.get(TrainingJobRow, job_id, populate_existing=True, with_for_update=True)
    if job is None or job.deleted_at is None or job.state not in TERMINAL_TRAINING_STATES:
        return []
    unresolved = await session.scalar(
        select(TrainingCheckpointRow.id)
        .where(
            TrainingCheckpointRow.job_id == job.id,
            TrainingCheckpointRow.state != "purged",
        )
        .limit(1)
    )
    if unresolved is not None:
        return []
    attempts = list(
        await session.scalars(select(TrainingAttemptRow).where(TrainingAttemptRow.job_id == job.id))
    )
    if any(attempt.state != "stopped" for attempt in attempts):
        return []
    if (
        await session.scalar(
            select(TrainingAdapterRow.id)
            .where(
                TrainingAdapterRow.source_job_id == job.id,
                TrainingAdapterRow.state.in_(["validating", "replicating"]),
            )
            .limit(1)
        )
        is not None
    ):
        return []
    commands = []
    for attempt in attempts:
        participants = list(
            await session.scalars(
                select(TrainingParticipantRow).where(
                    TrainingParticipantRow.attempt_id == attempt.id
                )
            )
        )
        for participant in participants:
            if participant.stop_proof is None:
                raise TrainingConflict("Attempt cleanup has no persisted participant death proof")
            node = await session.get(NodeRow, participant.node_id)
            assert node is not None
            identity = uuid.uuid5(participant.id, "attempt-cleanup")
            body = TrainingAttemptCleanupRequest(
                command_id=identity,
                job_id=job.id,
                attempt_id=attempt.id,
                fence=attempt.fence,
                node=node.name,
                prepared_command_id=participant.command_id,
            )
            prior = await session.get(TrainingCommandRow, identity)
            if prior is None:
                session.add(
                    TrainingCommandRow(
                        id=identity,
                        actor_user_id=job.owner_user_id,
                        operation="node.training.cleanup",
                        subject_id=str(participant.id),
                        job_id=job.id,
                        attempt_id=attempt.id,
                        idempotency_key=f"attempt-cleanup:{participant.id}",
                        request_sha256=payload_digest(body),
                        payload=body.model_dump(mode="json"),
                        state="pending",
                    )
                )
            elif prior.payload != body.model_dump(mode="json"):
                raise TrainingConflict("Immutable attempt cleanup intent changed")
            commands.append(identity)
    return commands


async def record_attempt_cleanup(
    session: AsyncSession,
    identity: uuid.UUID,
    receipt: TrainingAttemptCleanupReceipt,
) -> None:
    from coire_api.training.service import payload_digest

    command = await session.get(TrainingCommandRow, identity)
    if command is None or command.operation != "node.training.cleanup":
        raise TrainingConflict("Unknown attempt cleanup intent")
    job = await session.get(
        TrainingJobRow, command.job_id, populate_existing=True, with_for_update=True
    )
    if job is None or job.deleted_at is None or job.state not in TERMINAL_TRAINING_STATES:
        raise TrainingConflict("Cleanup job is not retired")
    command = await session.get(
        TrainingCommandRow, identity, populate_existing=True, with_for_update=True
    )
    assert command is not None
    request = TrainingAttemptCleanupRequest.model_validate(command.payload)
    if (
        not receipt.purged
        or receipt.command_id != identity
        or receipt.job_id != request.job_id
        or receipt.attempt_id != request.attempt_id
        or receipt.fence != request.fence
        or receipt.node != request.node
        or payload_digest(request) != command.request_sha256
    ):
        raise TrainingConflict("Attempt erasure proof differs from its scope")
    participant = await session.get(TrainingParticipantRow, uuid.UUID(command.subject_id))
    attempt = await session.get(TrainingAttemptRow, request.attempt_id)
    if (
        participant is None
        or attempt is None
        or attempt.state != "stopped"
        or participant.stop_proof is None
    ):
        raise TrainingConflict("Cleanup still has unresolved native ownership")
    hold = await session.get(
        TrainingStorageReservationRow, participant.disk_reservation_id, with_for_update=True
    )
    if hold is None or hold.subject_id != request.attempt_id or hold.node_id != participant.node_id:
        raise TrainingConflict("Cleanup disk hold identity changed")
    hold.state, hold.released_at = "released", datetime.now(UTC)
    command.receipt, command.state = receipt.model_dump(mode="json"), "succeeded"
