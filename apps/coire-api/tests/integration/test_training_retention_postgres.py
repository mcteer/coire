"""Real persisted two-copy cleanup, restart replay and retained recovery references."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_training_runtime_postgres import RuntimeDatabase
from test_training_runtime_postgres import runtime_db as runtime_db
from test_training_transactions import manifest as checkpoint_manifest
from training_measurement_fixtures import ATTEMPT, JOB

from coire_api.db import (
    EvaluationCheckpointPinRow,
    NodeRow,
    TrainingArtifactCopyRow,
    TrainingAttemptRow,
    TrainingCheckpointRow,
    TrainingCommandRow,
    TrainingEvaluationTriggerRow,
    TrainingJobRow,
    TrainingParticipantRow,
    TrainingStorageReservationRow,
)
from coire_api.training.retention import enqueue_checkpoint_cleanup, retention_candidates
from coire_core.errors import TrainingConflict
from coire_core.models.training_node import (
    TrainingArtifactDeleteRequest,
    TrainingArtifactDeletionReceipt,
)
from coire_scheduler.training_retention import TrainingRetentionWorker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1", reason="requires disposable Postgres"
    ),
]


async def seed(database: RuntimeDatabase, *, keep: int = 3) -> list[uuid.UUID]:
    factory, _ = database
    identities = [uuid.uuid4() for _ in range(5)]
    async with factory.begin() as session:
        nodes = list(await session.scalars(select(NodeRow)))
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        from coire_api.training.service import payload_digest
        from coire_core.models.training import ResolvedTrainingSpec

        resolved = ResolvedTrainingSpec.model_validate(job.resolved_spec)
        resolved.spec.output.keep_last_checkpoints = keep
        job.resolved_spec = resolved.model_dump(mode="json")
        job.resolved_sha256 = payload_digest(resolved)
        for step, identity in enumerate(identities, 1):
            artifact = checkpoint_manifest(step).model_copy(update={"artifact_id": identity})
            digest = artifact.canonical_sha256()
            session.add(
                TrainingCheckpointRow(
                    id=identity,
                    job_id=JOB,
                    attempt_id=ATTEMPT,
                    fence=1,
                    completed_update=step,
                    manifest_sha256=digest,
                    manifest=artifact.model_dump(mode="json"),
                    total_bytes=3,
                    state="committed",
                    committed_at=datetime.now(UTC),
                )
            )
            await session.flush()
            for node in nodes:
                session.add(
                    TrainingArtifactCopyRow(
                        artifact_id=identity,
                        checkpoint_id=identity,
                        node_id=node.id,
                        manifest_sha256=digest,
                        storage_key=str(identity),
                        total_bytes=3,
                        state="verified",
                        verified_at=datetime.now(UTC),
                    )
                )
        job.latest_checkpoint_id = identities[-1]
        await session.flush()
    return identities


async def test_evaluation_pin_survives_retention_until_safe_release(
    runtime_db: RuntimeDatabase,
) -> None:
    identities = await seed(runtime_db, keep=1)
    factory, _ = runtime_db
    async with factory.begin() as session:
        trigger = TrainingEvaluationTriggerRow(
            id=uuid.uuid4(),
            job_id=JOB,
            checkpoint_id=identities[0],
            boundary_kind="checkpoint",
            completed_update=1,
            schedule_sha256="a" * 64,
            schedules=[],
            phase="evaluating",
            fence=1,
            deadline_at=datetime.now(UTC),
        )
        session.add(trigger)
        await session.flush()
        pin = EvaluationCheckpointPinRow(trigger_id=trigger.id, checkpoint_id=identities[0])
        session.add(pin)
        await session.flush()
        candidates = await retention_candidates(session, JOB, keep=1, byte_limit=3)
        assert identities[0] not in {item.id for item in candidates}
        commands = await enqueue_checkpoint_cleanup(session, JOB)
        for command in (
            await session.scalars(
                select(TrainingCommandRow).where(TrainingCommandRow.id.in_(commands))
            )
        ).all():
            request = TrainingArtifactDeleteRequest.model_validate(command.payload)
            assert request.expected_manifest is not None
            assert request.expected_manifest.artifact_id != identities[0]
        pin.released_at = datetime.now(UTC)
        await session.flush()
        assert identities[0] in {
            item.id for item in await retention_candidates(session, JOB, keep=1)
        }


async def test_cleanup_keeps_latest_and_resume_reference_even_after_progress_rewind(
    runtime_db: RuntimeDatabase,
) -> None:
    identities = await seed(runtime_db, keep=1)
    factory, _ = runtime_db
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        assert job is not None and attempt is not None
        job.latest_checkpoint_id = identities[1]
        attempt.resume_checkpoint_id = identities[0]
        candidates = await retention_candidates(session, JOB, keep=1)
        assert {row.id for row in candidates} == {identities[2], identities[3]}
        # Highest update and the actual lower current recovery point both survive.
        commands = await enqueue_checkpoint_cleanup(session, JOB)
        rows = list(
            await session.scalars(
                select(TrainingCommandRow).where(TrainingCommandRow.id.in_(commands))
            )
        )
        assert rows and all(row.operation == "node.artifact.delete" for row in rows)
        retired = list(
            await session.scalars(
                select(TrainingArtifactCopyRow).where(TrainingArtifactCopyRow.state == "deleting")
            )
        )
        assert not {row.artifact_id for row in retired} & {
            identities[0],
            identities[1],
            identities[-1],
        }


async def test_two_copy_cleanup_replays_after_lost_ack_and_marks_purged_only_after_both_proofs(
    runtime_db: RuntimeDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = await seed(runtime_db)
    factory, _ = runtime_db

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        async with factory.begin() as session:
            yield session

    monkeypatch.setattr("coire_scheduler.training_retention.session_scope", sessions)
    async with factory.begin() as session:
        commands = await enqueue_checkpoint_cleanup(session, JOB)
        selected = list(
            await session.scalars(
                select(TrainingCommandRow).where(TrainingCommandRow.id.in_(commands))
            )
        )
        copies = {copy.id: copy for copy in await session.scalars(select(TrainingArtifactCopyRow))}
        pair = [
            row.id
            for row in selected
            if copies[uuid.UUID(row.subject_id)].artifact_id == identities[0]
        ]
        assert len(pair) == 2

    class Node:
        def __init__(self) -> None:
            self.lost = True
            self.receipts: dict[uuid.UUID, TrainingArtifactDeletionReceipt] = {}

        async def delete_training_artifact(
            self, node: str, artifact_id: uuid.UUID, request: TrainingArtifactDeleteRequest
        ) -> TrainingArtifactDeletionReceipt:
            receipt = self.receipts.setdefault(
                request.command_id,
                TrainingArtifactDeletionReceipt(
                    command_id=request.command_id,
                    artifact_id=artifact_id,
                    purged=True,
                ),
            )
            if self.lost:
                self.lost = False
                raise TimeoutError("synthetic lost acknowledgement")
            return receipt

    node = Node()
    worker = TrainingRetentionWorker(node)  # type: ignore[arg-type]
    with pytest.raises(TimeoutError):
        await worker.dispatch(pair[0])
    async with factory.begin() as session:
        row = await session.get(TrainingCommandRow, pair[0])
        checkpoint = await session.get(TrainingCheckpointRow, identities[0])
        assert row is not None and row.state == "dispatching"
        assert checkpoint is not None and checkpoint.purged_at is None
    await TrainingRetentionWorker(node).dispatch(pair[0])  # type: ignore[arg-type]
    async with factory.begin() as session:
        checkpoint = await session.get(TrainingCheckpointRow, identities[0])
        assert checkpoint is not None and checkpoint.state == "purging"
        from coire_api.training.checkpoints import checkpoint_detail

        projected = await checkpoint_detail(session, checkpoint)
        assert projected.state == "purging" and len(projected.verified_nodes) < 2
    await worker.dispatch(pair[1])
    await worker.dispatch(pair[1])
    async with factory.begin() as session:
        checkpoint = await session.get(TrainingCheckpointRow, identities[0])
        latest = await session.get(TrainingCheckpointRow, identities[-1])
        assert checkpoint is not None and checkpoint.state == "purged" and checkpoint.purged_at
        assert latest is not None and latest.state == "committed"


async def test_retired_job_cleanup_refuses_uncertain_execution_then_preserves_metadata(
    runtime_db: RuntimeDatabase,
) -> None:
    identities = await seed(runtime_db)
    factory, _ = runtime_db
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        job.state, job.deleted_at = "cancelled", datetime.now(UTC)
        with pytest.raises(TrainingConflict, match="unresolved"):
            await enqueue_checkpoint_cleanup(session, JOB)
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        assert attempt is not None
        attempt.state = "stopped"
        await session.flush()
        commands = await enqueue_checkpoint_cleanup(session, JOB)
        assert len(commands) == 2 * len(identities)
        assert await session.get(TrainingJobRow, JOB) is not None


async def test_workspace_hold_release_requires_scope_matched_purge_proof(
    runtime_db: RuntimeDatabase,
) -> None:
    from coire_api.training.retention import enqueue_attempt_cleanup, record_attempt_cleanup
    from coire_core.models.training_node import (
        TrainingAttemptCleanupReceipt,
        TrainingAttemptCleanupRequest,
        TrainingStopReceipt,
    )

    factory, prepared = runtime_db
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        participant = await session.scalar(
            select(TrainingParticipantRow).where(TrainingParticipantRow.attempt_id == ATTEMPT)
        )
        assert job is not None and attempt is not None and participant is not None
        job.state, job.deleted_at, attempt.state = "cancelled", datetime.now(UTC), "stopped"
        participant.stop_proof = TrainingStopReceipt(
            attempt_id=ATTEMPT,
            fence=attempt.fence,
            node=prepared.node,
            pid=None,
            process_create_time=None,
            stopped=True,
            observed_at=datetime.now(UTC),
        ).model_dump(mode="json")
        session.add(
            TrainingStorageReservationRow(
                id=participant.disk_reservation_id,
                owner_user_id=job.owner_user_id,
                node_id=participant.node_id,
                subject_id=ATTEMPT,
                bytes=1024,
                state="held",
            )
        )
        await session.flush()
        identities = await enqueue_attempt_cleanup(session, JOB)
        assert len(identities) == 1
        await session.flush()
        command = await session.get(TrainingCommandRow, identities[0])
        assert command is not None
        request = TrainingAttemptCleanupRequest.model_validate(command.payload)
        assert request.prepared_command_id == participant.command_id
        hold = await session.get(TrainingStorageReservationRow, participant.disk_reservation_id)
        assert hold is not None and hold.state == "held"
        proof = TrainingAttemptCleanupReceipt(
            command_id=request.command_id,
            job_id=JOB,
            attempt_id=ATTEMPT,
            fence=request.fence,
            node=request.node,
            purged=False,
        )
        with pytest.raises(TrainingConflict, match="proof"):
            await record_attempt_cleanup(session, request.command_id, proof)
        assert hold.state == "held"
        await record_attempt_cleanup(
            session, request.command_id, proof.model_copy(update={"purged": True})
        )
        assert hold.state == "released" and hold.released_at is not None
        assert command.state == "succeeded"


async def test_retirement_lane_replays_workspace_erasure_after_lost_ack(
    runtime_db: RuntimeDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_core.models.training_node import (
        TrainingAttemptCleanupReceipt,
        TrainingAttemptCleanupRequest,
        TrainingStopReceipt,
    )

    factory, prepared = runtime_db

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        async with factory.begin() as session:
            yield session

    monkeypatch.setattr("coire_scheduler.training_retention.session_scope", sessions)
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        participant = await session.scalar(
            select(TrainingParticipantRow).where(TrainingParticipantRow.attempt_id == ATTEMPT)
        )
        assert job is not None and attempt is not None and participant is not None
        job.state, job.deleted_at, attempt.state = "cancelled", datetime.now(UTC), "stopped"
        participant.stop_proof = TrainingStopReceipt(
            attempt_id=ATTEMPT,
            fence=attempt.fence,
            node=prepared.node,
            pid=None,
            process_create_time=None,
            stopped=True,
            observed_at=datetime.now(UTC),
        ).model_dump(mode="json")
        hold_id = participant.disk_reservation_id
        session.add(
            TrainingStorageReservationRow(
                id=hold_id,
                owner_user_id=job.owner_user_id,
                node_id=participant.node_id,
                subject_id=ATTEMPT,
                bytes=1024,
                state="held",
            )
        )

    class Node:
        def __init__(self) -> None:
            self.requests: list[TrainingAttemptCleanupRequest] = []

        async def cleanup_training_attempt(
            self, request: TrainingAttemptCleanupRequest
        ) -> TrainingAttemptCleanupReceipt:
            self.requests.append(request)
            if len(self.requests) == 1:
                raise TimeoutError("synthetic lost acknowledgement after native erasure")
            return TrainingAttemptCleanupReceipt(
                command_id=request.command_id,
                job_id=request.job_id,
                attempt_id=request.attempt_id,
                fence=request.fence,
                node=request.node,
                purged=True,
            )

    node = Node()
    await TrainingRetentionWorker(node).run_once()  # type: ignore[arg-type]
    async with factory.begin() as session:
        hold = await session.get(TrainingStorageReservationRow, hold_id)
        command = await session.get(TrainingCommandRow, node.requests[0].command_id)
        assert hold is not None and hold.state == "held"
        assert command is not None and command.state == "dispatching"
    await TrainingRetentionWorker(node).run_once()  # type: ignore[arg-type]
    assert len(node.requests) == 2 and node.requests[0] == node.requests[1]
    async with factory.begin() as session:
        hold = await session.get(TrainingStorageReservationRow, hold_id)
        command = await session.get(TrainingCommandRow, node.requests[0].command_id)
        assert hold is not None and hold.state == "released" and hold.released_at is not None
        assert command is not None and command.state == "succeeded"
        assert await session.get(TrainingJobRow, JOB) is not None
