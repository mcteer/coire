"""Restartable exact-copy cleanup; uncertain node erasure never frees retained state."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress

from sqlalchemy import select

from coire_api.db import (
    NodeRow,
    TrainingArtifactCopyRow,
    TrainingCommandRow,
    TrainingJobRow,
    session_scope,
)
from coire_api.training.retention import (
    enqueue_attempt_cleanup,
    enqueue_checkpoint_cleanup,
    expire_events,
    record_attempt_cleanup,
    record_deletion,
)
from coire_api.training.service import payload_digest
from coire_api.training.telemetry import observed
from coire_api.training_executor import TrainingNodeClient
from coire_core.errors import TrainingConflict
from coire_core.models.training_node import (
    TrainingArtifactDeleteRequest,
    TrainingAttemptCleanupRequest,
)

logger = logging.getLogger(__name__)


class TrainingRetentionWorker:
    def __init__(self, client: TrainingNodeClient) -> None:
        self.client = client
        self._cursor = ""
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="training-retention")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as error:
                logger.warning(
                    "training cleanup deferred", extra={"error_type": type(error).__name__}
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), 10)

    @observed("coire.scheduler.training.retention.scan")
    async def run_once(self) -> None:
        async with session_scope() as session:
            jobs = list(
                await session.scalars(
                    select(TrainingJobRow.id)
                    .where(
                        TrainingJobRow.id > self._cursor,
                        TrainingJobRow.resolved_spec.is_not(None),
                    )
                    .order_by(TrainingJobRow.id)
                    .limit(8)
                )
            )
            await expire_events(session)
        self._cursor = jobs[-1] if jobs else ""
        for job_id in jobs:
            try:
                async with session_scope() as session:
                    await enqueue_checkpoint_cleanup(session, job_id)
                    await enqueue_attempt_cleanup(session, job_id)
            except Exception as error:
                logger.info(
                    "checkpoint retention awaiting reconciliation",
                    extra={
                        "job_id": job_id,
                        "error_type": type(error).__name__,
                    },
                )
        async with session_scope() as session:
            commands = list(
                await session.scalars(
                    select(TrainingCommandRow.id)
                    .where(
                        TrainingCommandRow.operation.in_(
                            ["node.artifact.delete", "node.training.cleanup"]
                        ),
                        TrainingCommandRow.state.in_(["pending", "dispatching"]),
                    )
                    .order_by(TrainingCommandRow.created_at, TrainingCommandRow.id)
                    .limit(16)
                )
            )
        results = await asyncio.gather(
            *(self.dispatch(identity) for identity in commands),
            return_exceptions=True,
        )
        if any(isinstance(result, Exception) for result in results):
            logger.info("artifact cleanup pending", extra={"operation": "artifact.delete"})

    @observed("coire.scheduler.training.retention.delete")
    async def dispatch(self, identity: uuid.UUID) -> None:
        async with session_scope() as session:
            command = await session.get(TrainingCommandRow, identity)
            if command is None or command.state not in {"pending", "dispatching"}:
                return
            operation = command.operation
        if operation == "node.training.cleanup":
            await self.dispatch_attempt(identity)
            return
        async with session_scope() as session:
            command = await session.get(TrainingCommandRow, identity)
            assert command is not None
            copy = await session.get(TrainingArtifactCopyRow, uuid.UUID(command.subject_id))
            if copy is None or copy.state != "deleting":
                raise TrainingConflict("Cleanup intent has no retiring artifact copy")
            # Job first, then copy/command: the same order as promotion/resume and
            # receipt reduction. Retirement already removed this copy from readiness.
            job = await session.get(TrainingJobRow, command.job_id, with_for_update=True)
            if job is None:
                raise TrainingConflict("Cleanup job is unavailable")
            copy = await session.get(
                TrainingArtifactCopyRow,
                copy.id,
                populate_existing=True,
                with_for_update=True,
            )
            command = await session.get(
                TrainingCommandRow,
                identity,
                populate_existing=True,
                with_for_update=True,
            )
            assert copy is not None and command is not None
            if command.state == "succeeded":
                return
            request = TrainingArtifactDeleteRequest.model_validate(command.payload)
            if (
                command.operation != "node.artifact.delete"
                or request.command_id != identity
                or payload_digest(request) != command.request_sha256
                or request.manifest_sha256 != copy.manifest_sha256
                or copy.state != "deleting"
            ):
                raise TrainingConflict("Immutable cleanup command differs")
            node = await session.get(NodeRow, copy.node_id)
            if node is None:
                raise TrainingConflict("Cleanup node is unavailable")
            node_name, artifact_id, copy_id = node.name, copy.artifact_id, copy.id
            retired = job.deleted_at is not None
            imports = (
                list(
                    await session.scalars(
                        select(TrainingCommandRow).where(
                            TrainingCommandRow.subject_id == node_name,
                            TrainingCommandRow.operation.in_(
                                [
                                    "node.artifact.import",
                                    "node.adapter.import",
                                    "node.training.component-import",
                                ]
                            ),
                        )
                    )
                )
                if retired
                else []
            )
            scoped_imports = []
            for entry in imports:
                component = entry.payload.get("component")
                component_id = component.get("artifact_id") if isinstance(component, dict) else None
                if str(artifact_id) in {entry.payload.get("artifact_id"), component_id}:
                    scoped_imports.append(entry)
            imports = scoped_imports
            command.state = "dispatching"
        # No database row/admission lock is held across node filesystem I/O.
        async with asyncio.timeout(5):
            from coire_api.nodes_client import NodeError, NodeErrorKind

            for entry in imports:
                try:
                    if entry.operation == "node.training.component-import":
                        await self.client.cancel_rank_import(node_name, entry.id)
                    else:
                        await self.client.cancel_training_artifact_import(node_name, entry.id)
                except NodeError as error:
                    if error.kind is not NodeErrorKind.NOT_FOUND:
                        raise
            proof = await self.client.delete_training_artifact(node_name, artifact_id, request)
        async with session_scope() as session:
            await record_deletion(session, copy_id, proof)

    @observed("coire.scheduler.training.retention.attempt")
    async def dispatch_attempt(self, identity: uuid.UUID) -> None:
        async with session_scope() as session:
            command = await session.get(TrainingCommandRow, identity, with_for_update=True)
            if (
                command is None
                or command.operation != "node.training.cleanup"
                or command.state not in {"pending", "dispatching"}
            ):
                return
            request = TrainingAttemptCleanupRequest.model_validate(command.payload)
            if request.command_id != identity or payload_digest(request) != command.request_sha256:
                raise TrainingConflict("Attempt cleanup command changed")
            command.state = "dispatching"
        async with asyncio.timeout(5):
            proof = await self.client.cleanup_training_attempt(request)
        async with session_scope() as session:
            await record_attempt_cleanup(session, identity, proof)
