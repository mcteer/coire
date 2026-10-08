"""Restartable training reducer worker; PostgreSQL is the execution journal.

The injected transport owns authenticated node I/O. No transport call is made
inside a reducer transaction. A restart replays immutable command identities,
drains native mailboxes and observes process ownership before admitting recovery.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, suppress
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    NodeRow,
    TrainingAdapterRow,
    TrainingAttemptRow,
    TrainingCheckpointRow,
    TrainingCommandRow,
    TrainingEvaluationTriggerRow,
    TrainingJobRow,
    TrainingParticipantRow,
    TrainingProfileRow,
    session_scope,
)
from coire_api.training.adapters import finalize_serving_adapter, stage_serving_adapter
from coire_api.training.authorization import authorize_live_training_action
from coire_api.training.events import append_event, current_job
from coire_api.training.service import recheck_training_inputs
from coire_api.training.telemetry import observed
from coire_api.training_executor import TrainingNodeClient
from coire_core.errors import TrainingConflict, TrainingForbidden, TrainingValidationError
from coire_core.models.engine import EngineStatus
from coire_core.models.training import (
    TrainingProfile,
    TrainingReason,
    TrainingStateEvent,
    parse_resolved_training_spec,
)
from coire_core.models.training_node import (
    NodeTrainingEventPage,
    NodeTrainingStatus,
    TrainingArtifactManifest,
)
from coire_scheduler.training import (
    advance_training,
    enqueue_controls,
    ingest_training_event,
    observe_training,
)

logger = logging.getLogger(__name__)
type SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
ACTIVE = ("preparing", "running", "stopping", "unknown")
TERMINAL = ("succeeded", "failed", "cancelled")


class TrainingControllerTransport(Protocol):
    """Control-plane adapter. All methods must be bounded and idempotent.

    dispatch_command and mirror_checkpoint adapt existing training_executor
    functions. Extraction persists its native command under command_id before
    side effects, returns None while pending, and returns an authenticated shared
    manifest only on success. prepare_adapter mirrors/verifies that artifact and
    runs a reserved exact-target inference smoke; no asserted pass booleans.
    guard_reason uses measured per-target telemetry, never traffic estimates.
    """

    async def dispatch_command(self, command_id: uuid.UUID) -> None: ...

    async def training_events(
        self, node: str, attempt_id: str, after: int
    ) -> NodeTrainingEventPage: ...

    async def training_status(self, node: str, attempt_id: str) -> NodeTrainingStatus: ...

    async def mirror_checkpoint(self, checkpoint_id: uuid.UUID) -> bool: ...

    async def extract_final_adapter(
        self, checkpoint_id: uuid.UUID, adapter_id: uuid.UUID, command_id: uuid.UUID
    ) -> TrainingArtifactManifest | None: ...

    async def prepare_adapter(
        self, adapter_id: uuid.UUID
    ) -> tuple[uuid.UUID, EngineStatus] | None: ...

    async def guard_reason(self, attempt_id: str) -> TrainingReason | None: ...

    async def resume_profile(self, job_id: str) -> TrainingProfile | None: ...


class TrainingExecutorTransport:
    """Adapt the existing executor; feature-owned integrations are injected.

    The owner must close the supplied TrainingNodeClient after stopping the worker.
    None from extraction/smoke means work is durably pending, not successful.
    """

    def __init__(
        self,
        client: TrainingNodeClient,
        *,
        extract_final_adapter: Callable[
            [uuid.UUID, uuid.UUID, uuid.UUID], Awaitable[TrainingArtifactManifest | None]
        ],
        prepare_adapter: Callable[[uuid.UUID], Awaitable[tuple[uuid.UUID, EngineStatus] | None]],
        guard_reason: Callable[[str], Awaitable[TrainingReason | None]],
        resume_profile: Callable[[str], Awaitable[TrainingProfile | None]],
    ) -> None:
        self.client = client
        self._extract = extract_final_adapter
        self._prepare_adapter = prepare_adapter
        self._guard = guard_reason
        self._resume_profile = resume_profile

    async def dispatch_command(self, command_id: uuid.UUID) -> None:
        from coire_api.training_executor import dispatch_training_command

        await dispatch_training_command(command_id, self.client)

    async def training_events(
        self, node: str, attempt_id: str, after: int
    ) -> NodeTrainingEventPage:
        return await self.client.training_events(node, attempt_id, after)

    async def training_status(self, node: str, attempt_id: str) -> NodeTrainingStatus:
        return await self.client.training_status(node, attempt_id)

    async def mirror_checkpoint(self, checkpoint_id: uuid.UUID) -> bool:
        from coire_api.training_executor import mirror_checkpoint

        return await mirror_checkpoint(checkpoint_id, self.client)

    async def extract_final_adapter(
        self, checkpoint_id: uuid.UUID, adapter_id: uuid.UUID, command_id: uuid.UUID
    ) -> TrainingArtifactManifest | None:
        return await self._extract(checkpoint_id, adapter_id, command_id)

    async def prepare_adapter(self, adapter_id: uuid.UUID) -> tuple[uuid.UUID, EngineStatus] | None:
        return await self._prepare_adapter(adapter_id)

    async def guard_reason(self, attempt_id: str) -> TrainingReason | None:
        return await self._guard(attempt_id)

    async def resume_profile(self, job_id: str) -> TrainingProfile | None:
        return await self._resume_profile(job_id)


class TrainingController:
    """Worker-compatible start/stop plus a deterministic, testable run_once.

    Commands have their own durable identity/fences, so duplicate scans are safe.
    Each job is isolated from other jobs' transport failures. Stop/pause work is
    dispatched before observation/replication/extraction and renewed leases.
    """

    def __init__(
        self,
        transport: TrainingControllerTransport,
        *,
        sessions: SessionFactory = session_scope,
        poll_interval_s: float = 1.0,
        io_timeout_s: float = 5.0,
    ) -> None:
        if not 0 < poll_interval_s <= 5 or not 0 < io_timeout_s <= 5:
            raise ValueError("training controller requires bounded polling/control I/O")
        self.transport = transport
        self.sessions = sessions
        self.poll_interval_s = poll_interval_s
        self.io_timeout_s = io_timeout_s
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="training-controller")

    async def stop(self) -> None:
        self._stopping.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Cancelling this poller never proves native workers died. Their execution
        # leases expire locally; the next controller reconciles counted holds.

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.error("training controller scan failed", extra={"reason": "scan_failed"})
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_interval_s)

    @observed("coire.scheduler.training.controller.scan")
    async def run_once(self) -> None:
        from coire_api.db import TrainingEvictionIntentRow
        from coire_scheduler.training_recovery import restore_evictions

        async with self.sessions() as session:
            restoration_attempts = list(
                (
                    await session.scalars(
                        select(TrainingEvictionIntentRow.attempt_id)
                        .join(
                            TrainingAttemptRow,
                            TrainingAttemptRow.id == TrainingEvictionIntentRow.attempt_id,
                        )
                        .where(
                            TrainingEvictionIntentRow.restoration_state == "pending",
                            TrainingAttemptRow.state == "stopped",
                        )
                        .distinct()
                        .limit(8)
                    )
                ).all()
            )
        for attempt_id in restoration_attempts:
            async with self.sessions() as session:
                await restore_evictions(session, attempt_id)
        async with self.sessions() as session:
            jobs = list(
                (
                    await session.scalars(
                        select(TrainingJobRow.id)
                        .where(
                            TrainingJobRow.state.not_in(TERMINAL),
                            TrainingJobRow.deleted_at.is_(None),
                        )
                        .order_by(
                            case(
                                (TrainingJobRow.state == "cancelling", 0),
                                (TrainingJobRow.state == "pausing", 1),
                                else_=2,
                            ),
                            TrainingJobRow.created_at,
                            TrainingJobRow.id,
                        )
                        .limit(8)
                    )
                ).all()
            )
        # Jobs progress independently; a partition cannot delay another job's kill lane.
        await asyncio.gather(*(self._isolated_tick(job_id) for job_id in jobs))

    async def _isolated_tick(self, job_id: str) -> None:
        try:
            await self.tick(job_id)
        except Exception:
            logger.warning(
                "training controller tick deferred",
                extra={"job_id": job_id, "reason": "reconciliation_pending"},
            )

    async def _advance(self, job_id: str) -> None:
        try:
            async with self.sessions() as session:
                await advance_training(session, job_id)
        except (TrainingForbidden, TrainingValidationError, TrainingConflict):
            # Input/authority failure must commit teardown intent in a fresh
            # transaction, rather than rolling it back with the failed reducer.
            async with self.sessions() as session:
                job = await current_job(session, job_id, lock=True)
                if job.state not in (*TERMINAL, "paused", "cancelling"):
                    job.state, job.safe_reason = "cancelling", "invalid_input"
                    job.version += 1
                    job.updated_at = datetime.now(UTC)
                    await append_event(
                        session,
                        job.id,
                        TrainingStateEvent.model_validate(
                            {"kind": "state", "state": job.state, "reason": job.safe_reason}
                        ),
                    )
                await advance_training(session, job_id)

    @observed("coire.scheduler.training.controller.tick")
    async def tick(self, job_id: str) -> None:
        await self._resume_protective(job_id)
        await self._resume_automatic(job_id, origin="evaluation")
        await self._advance(job_id)
        await self._dispatch(job_id, controls_only=True)
        async with self.sessions() as session:
            attempts = list(
                (
                    await session.scalars(
                        select(TrainingAttemptRow.id).where(
                            TrainingAttemptRow.job_id == job_id,
                            TrainingAttemptRow.state.in_(ACTIVE),
                        )
                    )
                ).all()
            )
        for attempt_id in attempts:
            await self._observe(attempt_id)
        await self._advance(job_id)
        await self._dispatch(job_id, controls_only=False)
        await self._finalize(job_id)

    async def _resume_protective(self, job_id: str) -> None:
        await self._resume_automatic(job_id, origin="protective")

    async def _resume_automatic(self, job_id: str, *, origin: str) -> None:
        async with self.sessions() as session:
            job = await current_job(session, job_id)
            if (
                job.state != "paused"
                or job.pause_origin != origin
                or (
                    origin == "protective"
                    and job.updated_at + timedelta(seconds=60) > datetime.now(UTC)
                )
            ):
                return
            trigger_id = job.evaluation_pause_trigger_id if origin == "evaluation" else None
            if origin == "evaluation":
                trigger = await session.get(TrainingEvaluationTriggerRow, trigger_id)
                if (
                    trigger is None
                    or trigger.phase != "resume_pending"
                    or trigger.pause_version != job.version
                    or trigger.fence != job.fence
                    or trigger.checkpoint_id != job.latest_checkpoint_id
                ):
                    return
            attempt_id = await session.scalar(
                select(TrainingAttemptRow.id)
                .where(TrainingAttemptRow.job_id == job_id)
                .order_by(TrainingAttemptRow.generation.desc())
                .limit(1)
            )
        if attempt_id is None:
            return
        async with asyncio.timeout(self.io_timeout_s):
            # This hook must require fresh live telemetry including the sample
            # floor, and a NEW profile after latency invalidation, not return an
            # old asserted eligibility flag.
            profile = await self.transport.resume_profile(job_id)
            reason = await self.transport.guard_reason(attempt_id)
        if profile is None or reason is not None:
            return
        async with self.sessions() as session:
            job = await current_job(session, job_id)
            await authorize_live_training_action(
                session, Principal.model_validate(job.authorization_snapshot)
            )
            job = await current_job(session, job_id, lock=True)
            if job.state != "paused" or job.pause_origin != origin:
                return
            if origin == "evaluation":
                trigger = await session.get(
                    TrainingEvaluationTriggerRow,
                    trigger_id,
                    with_for_update=True,
                    populate_existing=True,
                )
                if (
                    trigger is None
                    or trigger.phase != "resume_pending"
                    or job.evaluation_pause_trigger_id != trigger_id
                    or trigger.pause_version != job.version
                    or trigger.fence != job.fence
                    or trigger.checkpoint_id != job.latest_checkpoint_id
                ):
                    return
            if (
                await session.scalar(
                    select(TrainingAttemptRow.id).where(
                        TrainingAttemptRow.job_id == job_id, TrainingAttemptRow.state.in_(ACTIVE)
                    )
                )
                is not None
            ):
                return
            row = await session.get(
                TrainingProfileRow, profile.id, populate_existing=True, with_for_update=True
            )
            if (
                row is None
                or row.invalidated_reason is not None
                or row.valid_until <= datetime.now(UTC)
                or row.report_sha256 != profile.report_sha256
                or profile.expires_at <= datetime.now(UTC)
            ):
                return
            resolved = parse_resolved_training_spec(job.resolved_spec)
            await recheck_training_inputs(session, resolved)
            nodes = list(
                (
                    await session.scalars(
                        select(NodeRow).where(NodeRow.name.in_(profile.request.nodes))
                    )
                ).all()
            )
            from coire_scheduler.training_guard import recheck_resource_evidence

            if await recheck_resource_evidence(session, job, nodes) is not None:
                return
            from coire_core.settings import get_settings

            remaining = (
                get_settings().training_execution_timeout_s - job.cumulative_execution_seconds
            )
            if remaining <= 0:
                return
            now = datetime.now(UTC)
            if origin == "evaluation":
                assert trigger is not None
                if not get_settings().training_enabled or now >= trigger.deadline_at + timedelta(
                    seconds=get_settings().evaluation_timeout_seconds
                    + get_settings().training_queue_timeout_s
                ):
                    return
                from coire_api.audit import write_principal_audit

                command_id = uuid.uuid5(trigger.id, "evaluation-resume")
                payload = {
                    "trigger_id": str(trigger.id),
                    "checkpoint_id": str(trigger.checkpoint_id),
                    "expected_version": job.version,
                    "fence": job.fence,
                    "profile_id": str(profile.id),
                    "profile_sha256": profile.report_sha256,
                }
                session.add(
                    TrainingCommandRow(
                        id=command_id,
                        actor_user_id=job.owner_user_id,
                        job_id=job.id,
                        subject_id=job.id,
                        operation="training.evaluation.resume",
                        idempotency_key=f"evaluation-resume:{trigger.id}",
                        request_sha256=hashlib.sha256(
                            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest(),
                        payload=payload,
                        state="succeeded",
                        receipt={"state": "queued", "version": job.version + 1},
                    )
                )
                await write_principal_audit(
                    session,
                    principal=Principal.model_validate(job.authorization_snapshot),
                    action="evaluation.training.resume",
                    target_type="training_job",
                    target_id=job.id,
                    context=payload,
                )
                trigger.phase, trigger.completed_at, trigger.resume_disposition = (
                    "complete",
                    now,
                    "resumed",
                )
                job.evaluation_pause_trigger_id = None
            job.state, job.pause_origin, job.safe_reason = "queued", None, None
            job.queue_deadline_at = now + timedelta(seconds=get_settings().training_queue_timeout_s)
            job.execution_deadline_at = job.queue_deadline_at + timedelta(seconds=remaining)
            job.version += 1
            job.updated_at = now
            await append_event(
                session,
                job.id,
                TrainingStateEvent.model_validate({"kind": "state", "state": "queued"}),
            )

    async def _dispatch(self, job_id: str, *, controls_only: bool) -> None:
        async with self.sessions() as session:
            operations = (
                ["node.training.stop", "node.training.pause"]
                if controls_only
                else [
                    "node.training.stop",
                    "node.training.pause",
                    "node.training.lease",
                    "node.training.checkpoint-commit",
                    "node.training.prepare",
                    "node.training.start",
                ]
            )
            commands = list(
                (
                    await session.scalars(
                        select(TrainingCommandRow.id)
                        .where(
                            TrainingCommandRow.job_id == job_id,
                            TrainingCommandRow.operation.in_(operations),
                            TrainingCommandRow.state.in_(["pending", "dispatching"]),
                        )
                        .order_by(
                            case(
                                (TrainingCommandRow.operation == "node.training.stop", 0), else_=1
                            ),
                            TrainingCommandRow.created_at,
                        )
                        .limit(32)
                    )
                ).all()
            )

        async def dispatch(command_id: uuid.UUID) -> None:
            try:
                async with asyncio.timeout(self.io_timeout_s):
                    await self.transport.dispatch_command(command_id)
            except Exception:
                # A timeout is uncertain delivery, not a failed/released hold.
                logger.info(
                    "training command awaiting reconciliation",
                    extra={"job_id": job_id, "command_id": str(command_id)},
                )

        await asyncio.gather(*(dispatch(command_id) for command_id in commands))

    async def _observe(self, attempt_id: str) -> None:
        async with self.sessions() as session:
            participants = list(
                (
                    await session.scalars(
                        select(NodeRow.name)
                        .join(TrainingParticipantRow, TrainingParticipantRow.node_id == NodeRow.id)
                        .where(TrainingParticipantRow.attempt_id == attempt_id)
                    )
                ).all()
            )
        # Every rank must be polled even if one is unreachable.
        await asyncio.gather(*(self._observe_node(node, attempt_id) for node in participants))
        async with asyncio.timeout(self.io_timeout_s):
            reason = await self.transport.guard_reason(attempt_id)
        if reason in {"memory_breach", "thermal_breach", "latency_breach"}:
            async with self.sessions() as session:
                attempt = await session.get(TrainingAttemptRow, attempt_id)
                assert attempt is not None
                job = await current_job(session, attempt.job_id, lock=True)
                if job.state == "running":
                    job.state, job.safe_reason, job.pause_origin = "pausing", reason, "protective"
                    job.version += 1
                    job.updated_at = datetime.now(UTC)
                    await append_event(
                        session,
                        job.id,
                        TrainingStateEvent.model_validate(
                            {"kind": "state", "state": "pausing", "reason": reason}
                        ),
                    )
                    await enqueue_controls(session, attempt.id, "pause")

    async def _observe_node(self, node: str, attempt_id: str) -> None:
        async with self.sessions() as session:
            attempt = await session.get(TrainingAttemptRow, attempt_id)
            if attempt is not None and attempt.state == "preparing":
                job = await current_job(session, attempt.job_id)
                prepare = await session.scalar(
                    select(TrainingCommandRow).where(
                        TrainingCommandRow.operation == "node.training.prepare",
                        TrainingCommandRow.attempt_id == attempt_id,
                        TrainingCommandRow.subject_id == node,
                    )
                )
                if job.state == "reserving" and (prepare is None or prepare.state == "pending"):
                    # The durable dispatcher commits dispatching before node I/O.
                    # Before idle drains finish, preparation does not exist yet.
                    # An unsent preparation has no native mailbox/status yet. Keep
                    # its counted hold and let dispatch run; do not initiate recovery.
                    return
        try:
            async with self.sessions() as session:
                cursor = int(
                    await session.scalar(
                        select(
                            func.coalesce(
                                func.max(TrainingCommandRow.payload["sequence"].as_integer()), 0
                            )
                        ).where(
                            TrainingCommandRow.operation == "node.training.event",
                            TrainingCommandRow.attempt_id == attempt_id,
                            TrainingCommandRow.subject_id == node,
                        )
                    )
                    or 0
                )
            async with asyncio.timeout(self.io_timeout_s):
                page = await self.transport.training_events(node, attempt_id, cursor)
            if page.next_sequence != (page.items[-1].sequence if page.items else cursor):
                raise TrainingConflict("Node mailbox cursor differs from persisted page")
            for event in page.items:
                if event.attempt_id != attempt_id:
                    raise TrainingConflict("Mailbox event identifies another attempt")
                async with self.sessions() as session:
                    await ingest_training_event(session, node, event)
            # Commit full mirrored bundles before reducing stop status: a worker
            # finishing its final callback may already report stopped on this tick.
            async with self.sessions() as session:
                checkpoints = list(
                    (
                        await session.scalars(
                            select(TrainingCheckpointRow.id).where(
                                TrainingCheckpointRow.attempt_id == attempt_id,
                                TrainingCheckpointRow.state.in_(["staging", "replicating"]),
                            )
                        )
                    ).all()
                )
            for checkpoint_id in checkpoints:
                async with asyncio.timeout(self.io_timeout_s):
                    await self.transport.mirror_checkpoint(checkpoint_id)
        except Exception:
            logger.info(
                "training mailbox awaiting reconciliation",
                extra={"attempt_id": attempt_id, "node": node},
            )
        try:
            async with asyncio.timeout(self.io_timeout_s):
                status = await self.transport.training_status(node, attempt_id)
            if status.node != node or status.attempt_id != attempt_id:
                raise TrainingConflict("Authenticated status identifies another participant")
            async with self.sessions() as session:
                await observe_training(session, status)
        except Exception:
            # Preserve an already pending cancel/pause. Unknown liveness never
            # frees memory and never creates another generation.
            logger.info(
                "training liveness remains unconfirmed",
                extra={"attempt_id": attempt_id, "node": node},
            )
            async with self.sessions() as session:
                attempt = await session.get(TrainingAttemptRow, attempt_id)
                if attempt is not None:
                    job = await current_job(session, attempt.job_id, lock=True)
                    attempt = await session.get(
                        TrainingAttemptRow, attempt_id, populate_existing=True, with_for_update=True
                    )
                    assert attempt is not None
                    if attempt.state in ACTIVE:
                        attempt.state = "unknown"
                        if job.state in {"reserving", "running"}:
                            job.state, job.safe_reason = "recovering", "node_unreachable"
                            job.version += 1
                            job.updated_at = datetime.now(UTC)

    async def _finalize(self, job_id: str) -> None:
        async with self.sessions() as session:
            job = await current_job(session, job_id)
            if job.state != "finalizing" or job.latest_checkpoint_id is None:
                return
            await authorize_live_training_action(
                session, Principal.model_validate(job.authorization_snapshot)
            )
            job = await current_job(session, job_id, lock=True)
            if job.state != "finalizing" or job.latest_checkpoint_id is None:
                return
            checkpoint_id = job.latest_checkpoint_id
            checkpoint = await session.get(TrainingCheckpointRow, checkpoint_id)
            attempt = (
                await session.get(TrainingAttemptRow, checkpoint.attempt_id) if checkpoint else None
            )
            resolved = parse_resolved_training_spec(job.resolved_spec)
            if (
                checkpoint is None
                or checkpoint.state != "committed"
                or checkpoint.fence != job.fence
                or checkpoint.completed_update != resolved.spec.optim.updates
                or attempt is None
                or attempt.state != "stopped"
            ):
                raise TrainingConflict("Final extraction needs completed training and proven stop")
            adapter_id = uuid.uuid5(checkpoint_id, "automatic-adapter")
            command_id = uuid.uuid5(checkpoint_id, "automatic-extraction")
            adapter = await session.get(TrainingAdapterRow, adapter_id)
            extract = adapter is None
            if extract:
                prior = await session.get(TrainingCommandRow, command_id)
                if prior is not None and prior.created_at + timedelta(seconds=60) <= datetime.now(
                    UTC
                ):
                    job.state, job.safe_reason, job.finished_at = (
                        "failed",
                        "replication_failed",
                        datetime.now(UTC),
                    )
                    job.version += 1
                    await append_event(
                        session,
                        job.id,
                        TrainingStateEvent.model_validate(
                            {"kind": "terminal", "state": job.state, "reason": job.safe_reason}
                        ),
                    )
                    return
                if prior is None:
                    payload = {"checkpoint_id": str(checkpoint_id), "adapter_id": str(adapter_id)}
                    session.add(
                        TrainingCommandRow(
                            id=command_id,
                            actor_user_id=job.owner_user_id,
                            idempotency_key=f"automatic-extraction:{checkpoint_id}",
                            operation="training.final.extract",
                            subject_id=str(adapter_id),
                            job_id=job.id,
                            request_sha256=hashlib.sha256(
                                json.dumps(payload, sort_keys=True).encode()
                            ).hexdigest(),
                            payload=payload,
                            state="dispatching",
                        )
                    )
        if extract:
            async with asyncio.timeout(self.io_timeout_s):
                manifest = await self.transport.extract_final_adapter(
                    checkpoint_id, adapter_id, command_id
                )
            if manifest is None:
                return
            if manifest.artifact_id != adapter_id:
                raise TrainingConflict("Extraction returned a different automatic adapter")
            async with self.sessions() as session:
                job = await current_job(session, job_id, lock=True)
                await stage_serving_adapter(
                    session,
                    Principal.model_validate(job.authorization_snapshot),
                    checkpoint_id,
                    manifest,
                    slug=job.output_slug,
                    automatic=True,
                )
                row = await session.get(TrainingCommandRow, command_id, with_for_update=True)
                assert row is not None
                row.receipt, row.state = manifest.model_dump(mode="json"), "succeeded"
        async with asyncio.timeout(self.io_timeout_s):
            smoke = await self.transport.prepare_adapter(adapter_id)
        if smoke is not None:
            instance_id, status = smoke
            async with self.sessions() as session:
                await finalize_serving_adapter(session, adapter_id, status, instance_id=instance_id)
