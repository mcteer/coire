"""Freeze declared recipe suites and Studio runtime before accepting v2 training."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    TrainingAdapterRow,
    TrainingCheckpointRow,
    TrainingEvaluationTriggerRow,
    TrainingJobRow,
)
from coire_api.evaluation.authorization import (
    authorize_live_evaluation_action,
    reject_self_judge,
    resolve_evaluation_target,
)
from coire_api.evaluation.catalog import get_row, project
from coire_api.evaluation.service import validate_subjects
from coire_api.evaluation.telemetry import observed
from coire_api.nodes_client import NodeClient, NodeError
from coire_core.errors import (
    EvaluationDisabled,
    EvaluationValidationError,
    TrainingConflict,
    TrainingUnavailable,
)
from coire_core.models.evaluation import EvaluationSubject, EvaluationTarget, SuiteKind
from coire_core.models.training import TrainingSpecV2
from coire_core.models.training_evaluation import ResolvedTrainingSuite
from coire_core.settings import Settings


@observed("coire.api.evaluation.training.resolve")
async def resolve_declarations(
    session: AsyncSession,
    principal: Principal,
    spec: TrainingSpecV2,
    *,
    base_manifest_sha256: str,
    settings: Settings,
) -> tuple[EvaluationTarget, list[ResolvedTrainingSuite]]:
    if not settings.evaluations_enabled:
        raise EvaluationDisabled()
    await authorize_live_evaluation_action(session, principal)
    async with NodeClient(settings) as client:
        try:
            for node in ("coire-edge-a", "coire-edge-b"):
                status = await client.health(node)
                training = getattr(status, "training_capabilities", None)
                if (
                    training is None
                    or 2 not in training.spec_versions
                    or (
                        any(item.checkpoint_updates for item in spec.eval.suites)
                        and 1 not in training.evaluation_checkpoint_ack_versions
                    )
                ):
                    raise TrainingUnavailable(
                        "Both Studios must support the declared training document and checkpoint decisions"
                    )
                evaluations = await client.evaluation_capabilities(node)
                if 1 not in evaluations.workload_versions:
                    raise TrainingUnavailable("Both Studios must support evaluation workloads")
            base = await resolve_evaluation_target(
                session,
                principal,
                EvaluationSubject(model_id=spec.model.model_id, variant_id=spec.model.variant_id),
                client,
            )
            if base.target.base_manifest_sha256 != base_manifest_sha256:
                raise TrainingConflict("Evaluation base differs from acquired training identity")
            declarations: list[ResolvedTrainingSuite] = []
            for schedule in spec.eval.suites:
                suite = project(
                    await get_row(session, schedule.suite_id, schedule.suite_version, admit=True)
                )
                if suite.template.kind is SuiteKind.HARNESS:
                    raise EvaluationValidationError("Training recipes declare task or judge suites")
                reject_self_judge([base.target], suite.judge.target if suite.judge else None)
                if suite.judge is not None:
                    actual = await resolve_evaluation_target(
                        session,
                        principal,
                        EvaluationSubject(
                            model_id=suite.judge.target.model_id,
                            variant_id=suite.judge.target.variant_id,
                            adapter_id=suite.judge.target.adapter_id,
                        ),
                        client,
                    )
                    if (
                        actual.target,
                        actual.runtime,
                        actual.capability_profile,
                        actual.template_override,
                        actual.context_window,
                    ) != (
                        suite.judge.target,
                        suite.judge.runtime,
                        suite.judge.capability_profile,
                        suite.judge.template_override,
                        suite.judge.context_window,
                    ):
                        raise TrainingConflict(
                            "Declared judge runtime differs from its frozen suite version"
                        )
                declarations.append(ResolvedTrainingSuite(schedule=schedule, suite=suite))
        except NodeError:
            raise TrainingUnavailable("Studio evaluation capabilities are unavailable") from None
    return base, declarations


@observed("coire.api.evaluation.training.final")
async def ensure_final_trigger(
    session: AsyncSession,
    job: TrainingJobRow,
    adapter: TrainingAdapterRow,
    *,
    settings: Settings,
) -> TrainingEvaluationTriggerRow | None:
    """Caller holds the job lock; commit the obligation with adapter/job success.

    Queue, admission switches and downstream evaluation outcomes cannot suppress
    this metadata obligation or rewrite successful training.
    """
    import hashlib
    import json
    import uuid
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from coire_api.audit import write_principal_audit
    from coire_api.db import TrainingCheckpointRow
    from coire_api.training.service import payload_digest
    from coire_core.models.training import ResolvedTrainingSpecV2, parse_resolved_training_spec

    if job.state != "succeeded" or adapter.state != "ready" or job.resolved_spec is None:
        return None
    resolved = parse_resolved_training_spec(job.resolved_spec)
    if not isinstance(resolved, ResolvedTrainingSpecV2):
        return None
    if (
        job.adapter_id != adapter.id
        or adapter.source_job_id != job.id
        or adapter.metadata_record.get("automatic") is not True
        or adapter.source_checkpoint_id is None
        or job.completed_update != resolved.spec.optim.updates
        or payload_digest(resolved) != job.resolved_sha256
    ):
        raise TrainingConflict("Final evaluation obligation differs from training success")
    checkpoint = await session.get(TrainingCheckpointRow, adapter.source_checkpoint_id)
    if (
        checkpoint is None
        or checkpoint.job_id != job.id
        or checkpoint.state != "committed"
        or checkpoint.completed_update != job.completed_update
        or checkpoint.fence != job.fence
    ):
        raise TrainingConflict("Final evaluation requires the complete final checkpoint")
    schedules = [item.model_dump(mode="json") for item in resolved.evaluations]
    digest = hashlib.sha256(
        json.dumps(
            schedules, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()
    existing = await session.scalar(
        select(TrainingEvaluationTriggerRow).where(
            TrainingEvaluationTriggerRow.job_id == job.id,
            TrainingEvaluationTriggerRow.boundary_kind == "final",
            TrainingEvaluationTriggerRow.completed_update == job.completed_update,
            TrainingEvaluationTriggerRow.schedule_sha256 == digest,
        )
    )
    if existing is not None:
        return existing
    now = datetime.now(UTC)
    trigger = TrainingEvaluationTriggerRow(
        id=uuid.uuid4(),
        job_id=job.id,
        checkpoint_id=checkpoint.id,
        boundary_kind="final",
        completed_update=job.completed_update,
        schedule_sha256=digest,
        schedules=schedules,
        phase="preparing_adapter",
        fence=job.fence,
        deadline_at=now + timedelta(seconds=settings.evaluation_queue_timeout_seconds),
        created_at=now,
    )
    session.add(trigger)
    await write_principal_audit(
        session,
        principal=Principal.model_validate(job.authorization_snapshot),
        action="evaluation.training.final",
        target_type="training_evaluation_trigger",
        target_id=str(trigger.id),
        context={"job_id": job.id, "adapter_id": str(adapter.id), "schedule_sha256": digest},
    )
    await session.flush()
    return trigger


@observed("coire.scheduler.evaluation.training.reconcile")
async def reconcile_final_trigger(
    session: AsyncSession, identity: uuid.UUID, *, settings: Settings
) -> bool:
    return await reconcile_trigger(session, identity, settings=settings, boundary="final")


async def reconcile_trigger(
    session: AsyncSession, identity: uuid.UUID, *, settings: Settings, boundary: str
) -> bool:
    """Admit all declared suites together, or record bounded explicit failures."""
    import hashlib
    import json
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select, text

    from coire_api.audit import write_principal_audit
    from coire_api.db import EvaluationGroupRow, EvaluationRunRow
    from coire_api.evaluation.events import append
    from coire_api.evaluation.evidence import reserve_quota
    from coire_api.evaluation.finalizer import finalize
    from coire_api.training.service import payload_digest, training_id
    from coire_core.errors import EvaluationForbidden
    from coire_core.models.evaluation import (
        MAX_EVALUATION_BYTES,
        TERMINAL_EVALUATION_STATES,
        EvaluationReason,
    )
    from coire_core.models.training import ResolvedTrainingSpecV2, parse_resolved_training_spec
    from coire_core.models.training_evaluation import ResolvedTrainingSuite

    snapshot = await session.get(TrainingEvaluationTriggerRow, identity)
    if snapshot is None:
        return True
    parent = await session.get(TrainingJobRow, snapshot.job_id)
    if parent is None:
        return False
    principal = Principal.model_validate(parent.authorization_snapshot)
    revoked = False
    try:
        await authorize_live_evaluation_action(session, principal)
    except EvaluationForbidden:
        revoked = True
    job = await session.get(
        TrainingJobRow, snapshot.job_id, with_for_update=True, populate_existing=True
    )
    trigger = await session.get(
        TrainingEvaluationTriggerRow, identity, with_for_update=True, populate_existing=True
    )
    if job is None or trigger is None or trigger.boundary_kind != boundary:
        return False
    if trigger.phase == "complete":
        return True
    if boundary == "checkpoint" and trigger.phase in {"cleaning_adapter", "resume_pending"}:
        return False
    if trigger.group_id is not None:
        runs = list(
            (
                await session.scalars(
                    select(EvaluationRunRow).where(EvaluationRunRow.group_id == trigger.group_id)
                )
            ).all()
        )
        if runs and all(
            run.state in TERMINAL_EVALUATION_STATES and run.cleanup_state == "complete"
            for run in runs
        ):
            if boundary == "checkpoint":
                trigger.phase = "cleaning_adapter"
                return False
            trigger.phase, trigger.completed_at = "complete", datetime.now(UTC)
            return True
        return False
    resolved = parse_resolved_training_spec(job.resolved_spec)
    if (
        not isinstance(resolved, ResolvedTrainingSpecV2)
        or payload_digest(resolved) != job.resolved_sha256
    ):
        raise TrainingConflict("Final obligation resolved lineage changed")
    adapter_id = (
        job.adapter_id if boundary == "final" else uuid.uuid5(trigger.id, "checkpoint-adapter")
    )
    adapter = await session.get(TrainingAdapterRow, adapter_id) if adapter_id else None
    owned = True
    if boundary == "checkpoint":
        try:
            await require_checkpoint_pause(session, job, trigger.id)
        except TrainingConflict:
            owned = False
        if (
            owned
            and not revoked
            and datetime.now(UTC) < trigger.deadline_at
            and (adapter is None or adapter.state != "ready")
        ):
            return False
        if (
            owned
            and not revoked
            and datetime.now(UTC) < trigger.deadline_at
            and adapter is not None
        ):
            from coire_api.db import EngineProcessRow, InstanceMemberRow, ModelInstanceRow
            from coire_core.models.engine import EngineState
            from coire_core.models.instance import InstanceState

            smoke_id = uuid.uuid5(adapter.id, "validation-smoke")
            smoke = await session.get(ModelInstanceRow, smoke_id)
            unsafe = await session.scalar(
                select(EngineProcessRow.id)
                .join(InstanceMemberRow, InstanceMemberRow.engine_id == EngineProcessRow.id)
                .where(
                    InstanceMemberRow.instance_id == smoke_id,
                    EngineProcessRow.state != EngineState.STOPPED,
                )
                .limit(1)
            )
            if smoke is not None and (
                smoke.state not in {InstanceState.STOPPED, InstanceState.FAILED}
                or unsafe is not None
            ):
                return False
    if boundary == "final" and (
        adapter is None
        or adapter.manifest_sha256 is None
        or adapter.source_checkpoint_id != trigger.checkpoint_id
    ):
        raise TrainingConflict("Final obligation adapter lineage is unavailable")
    subjects = [resolved.evaluation_base]
    if adapter is not None and adapter.manifest_sha256 is not None:
        if adapter.source_checkpoint_id != trigger.checkpoint_id:
            raise TrainingConflict("Obligation adapter lineage is unavailable")
        candidate = EvaluationTarget.model_validate(
            {
                **resolved.evaluation_base.model_dump(mode="json"),
                "public_selector": adapter.selector,
                "target": {
                    **resolved.evaluation_base.target.model_dump(mode="json"),
                    "adapter_id": adapter.id,
                    "adapter_manifest_sha256": adapter.manifest_sha256,
                },
                "display_name": f"{resolved.evaluation_base.display_name}@{adapter.slug}",
            }
        )
        subjects.append(candidate)
    schedules = [ResolvedTrainingSuite.model_validate(value) for value in trigger.schedules]
    expected = (
        resolved.evaluations
        if boundary == "final"
        else [
            item
            for item in resolved.evaluations
            if trigger.completed_update in item.schedule.checkpoint_updates
        ]
    )
    if schedules != expected:
        raise TrainingConflict("Final obligation schedule differs from frozen training")
    await session.execute(text("SELECT pg_advisory_xact_lock(170017)"))
    await session.execute(text("SELECT pg_advisory_xact_lock(170027)"))
    pending = int(
        await session.scalar(
            select(func.count())
            .select_from(EvaluationRunRow)
            .where(
                EvaluationRunRow.state.notin_([state.value for state in TERMINAL_EVALUATION_STATES])
            )
        )
        or 0
    )
    quota = int(
        await session.scalar(
            select(func.coalesce(func.sum(EvaluationRunRow.evidence_reserved_bytes), 0))
        )
        or 0
    )
    capacity = (
        pending + len(schedules) <= settings.evaluation_max_pending_runs
        and quota + len(schedules) * MAX_EVALUATION_BYTES
        <= settings.evaluation_evidence_quota_bytes
    )
    now = datetime.now(UTC)
    reason: EvaluationReason | None = (
        "authorization_revoked"
        if revoked
        else "admission_disabled"
        if now >= trigger.deadline_at and not settings.evaluations_enabled
        else "model_unavailable"
        if adapter is None
        or adapter.state != "ready"
        or job.deleted_at is not None
        or (job.state != "succeeded" if boundary == "final" else not owned)
        else None
    )
    if reason is None and now >= trigger.deadline_at:
        reason = "admission_disabled" if not settings.evaluations_enabled else "capacity_timeout"
    if reason is None and (not settings.evaluations_enabled or not capacity):
        return False
    group = EvaluationGroupRow(
        id=training_id(),
        owner_user_id=job.owner_user_id,
        origin="training_final" if boundary == "final" else "training_checkpoint",
        job_id=job.id,
        checkpoint_id=trigger.checkpoint_id,
        completed_update=trigger.completed_update,
        subjects=[subject.model_dump(mode="json") for subject in subjects],
        created_at=now,
    )
    session.add(group)
    await session.flush()
    trigger.group_id, trigger.phase = group.id, "evaluating"
    created: list[EvaluationRunRow] = []
    for declaration in schedules:
        catalog = await get_row(session, declaration.suite.suite_id, declaration.suite.version)
        if catalog.content_sha256 != declaration.suite.content_sha256:
            raise TrainingConflict("Declared catalog identity changed")
        if reason is None:
            validate_subjects(declaration.suite, subjects)
        digest = hashlib.sha256(
            json.dumps(
                {
                    "trigger_id": str(trigger.id),
                    "suite": declaration.suite.model_dump(mode="json"),
                    "subjects": group.subjects,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        run = EvaluationRunRow(
            id=training_id(),
            group_id=group.id,
            owner_user_id=job.owner_user_id,
            suite_row_id=catalog.id,
            suite_snapshot=declaration.suite.model_dump(mode="json"),
            subjects=group.subjects,
            authorization_snapshot=job.authorization_snapshot,
            request_sha256=digest,
            idempotency_key_sha256=hashlib.sha256(
                f"training-{boundary}:{trigger.id}:{catalog.id}".encode()
            ).hexdigest(),
            state="queued",
            fence=1,
            version=1,
            next_event_sequence=1,
            cleanup_state="complete",
            evidence_reserved_bytes=0,
            queue_deadline_at=trigger.deadline_at,
            execution_deadline_at=trigger.deadline_at
            + timedelta(seconds=declaration.suite.timeout_seconds),
            created_at=now,
            data_snapshot={
                "job_id": job.id,
                "checkpoint_id": str(trigger.checkpoint_id),
                "completed_update": trigger.completed_update,
                "resolved_spec": resolved.model_dump(mode="json"),
                "resolved_sha256": job.resolved_sha256,
            },
        )
        session.add(run)
        await session.flush()
        if reason is None:
            await reserve_quota(session, run, settings)
        created.append(run)
    # Publish every member before group events; partial groups cannot terminate early.
    await session.flush()
    for run in created:
        await append(session, run)
        if reason is not None:
            await finalize(
                session,
                run.id,
                fence=run.fence,
                outcome="timed_out" if reason == "capacity_timeout" else "failed",
                reason=reason,
            )
    await write_principal_audit(
        session,
        principal=principal,
        action="evaluation.training.dispatch",
        target_type="training_evaluation_trigger",
        target_id=str(trigger.id),
        context={"job_id": job.id, "group_id": group.id, "reason": reason},
    )
    if reason is not None:
        if boundary == "checkpoint":
            trigger.phase = "cleaning_adapter"
            return False
        trigger.phase, trigger.completed_at = "complete", now
        return True
    return False


@observed("coire.api.evaluation.training.checkpoint")
async def ensure_checkpoint_trigger(
    session: AsyncSession,
    job: TrainingJobRow,
    checkpoint: TrainingCheckpointRow,
    *,
    settings: Settings,
) -> TrainingEvaluationTriggerRow | None:
    """Caller holds the job lock and has proved both full mirrored copies."""
    import hashlib
    import json
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from coire_api.audit import write_principal_audit
    from coire_api.db import EvaluationCheckpointPinRow, TrainingCommandRow
    from coire_api.training.events import append_event
    from coire_api.training.service import payload_digest
    from coire_core.models.training import (
        ResolvedTrainingSpecV2,
        TrainingStateEvent,
        parse_resolved_training_spec,
    )
    from coire_core.models.training_node import TrainingArtifactManifest

    if job.resolved_spec is None:
        return None
    resolved = parse_resolved_training_spec(job.resolved_spec)
    if not isinstance(resolved, ResolvedTrainingSpecV2):
        return None
    declarations = [
        item
        for item in resolved.evaluations
        if checkpoint.completed_update in item.schedule.checkpoint_updates
    ]
    if not declarations:
        return None
    if payload_digest(resolved) != job.resolved_sha256:
        raise TrainingConflict("Evaluation checkpoint resolved lineage differs")
    schedules = [item.model_dump(mode="json") for item in declarations]
    digest = hashlib.sha256(
        json.dumps(
            schedules, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()
    existing = await session.scalar(
        select(TrainingEvaluationTriggerRow).where(
            TrainingEvaluationTriggerRow.job_id == job.id,
            TrainingEvaluationTriggerRow.boundary_kind == "checkpoint",
            TrainingEvaluationTriggerRow.completed_update == checkpoint.completed_update,
            TrainingEvaluationTriggerRow.schedule_sha256 == digest,
        )
    )
    if existing is not None:
        return existing
    unfinished = await session.scalar(
        select(TrainingEvaluationTriggerRow.id)
        .where(
            TrainingEvaluationTriggerRow.job_id == job.id,
            TrainingEvaluationTriggerRow.boundary_kind == "checkpoint",
            TrainingEvaluationTriggerRow.phase != "complete",
        )
        .limit(1)
    )
    if unfinished is not None:
        raise TrainingConflict("An earlier evaluation boundary still owns checkpoint capacity")
    manifest = TrainingArtifactManifest.model_validate(checkpoint.manifest)
    from coire_api.db import TrainingAttemptRow

    attempt = await session.get(TrainingAttemptRow, checkpoint.attempt_id)
    if (
        checkpoint.state != "committed"
        or checkpoint.job_id != job.id
        or checkpoint.fence != job.fence
        or manifest.job_id != job.id
        or manifest.attempt_id != checkpoint.attempt_id
        or manifest.fence != job.fence
        or manifest.update != checkpoint.completed_update
        or manifest.canonical_sha256() != checkpoint.manifest_sha256
        or manifest.resolved_spec_sha256 != job.resolved_sha256
        or attempt is None
        or attempt.job_id != job.id
        or attempt.fence != job.fence
        or manifest.world_size != attempt.world_size
        or manifest.runtime_sha256 != attempt.runtime_sha256
        or manifest.runtime_sha256 != resolved.runtime_sha256
    ):
        raise TrainingConflict("Evaluation boundary is not the exact full committed checkpoint")
    from coire_api.training.checkpoints import verified_nodes

    if await verified_nodes(
        session, checkpoint.id, checkpoint.manifest_sha256, checkpoint.total_bytes
    ) != {"coire-edge-a", "coire-edge-b"}:
        raise TrainingConflict("Evaluation boundary awaits independently verified full copies")
    now = datetime.now(UTC)
    trigger = TrainingEvaluationTriggerRow(
        id=uuid.uuid4(),
        job_id=job.id,
        checkpoint_id=checkpoint.id,
        boundary_kind="checkpoint",
        completed_update=checkpoint.completed_update,
        schedule_sha256=digest,
        schedules=schedules,
        phase="pending_pause",
        fence=job.fence,
        deadline_at=now + timedelta(seconds=settings.evaluation_queue_timeout_seconds),
    )
    session.add(trigger)
    await session.flush()
    session.add(EvaluationCheckpointPinRow(trigger_id=trigger.id, checkpoint_id=checkpoint.id))
    if job.state == "running" and job.pause_origin is None:
        job.state, job.pause_origin, job.safe_reason = "pausing", "evaluation", "evaluation_pending"
        job.evaluation_pause_trigger_id = trigger.id
        job.version += 1
        job.updated_at = now
        trigger.pause_version = job.version
        command_id = uuid.uuid5(trigger.id, "evaluation-pause")
        session.add(
            TrainingCommandRow(
                id=command_id,
                actor_user_id=job.owner_user_id,
                idempotency_key=f"evaluation-pause:{trigger.id}",
                operation="training.evaluation.pause",
                subject_id=job.id,
                job_id=job.id,
                attempt_id=checkpoint.attempt_id,
                request_sha256=digest,
                payload={
                    "trigger_id": str(trigger.id),
                    "checkpoint_id": str(checkpoint.id),
                    "job_version": job.version,
                },
                receipt={"state": "pausing", "version": job.version},
                state="succeeded",
            )
        )
        await append_event(
            session,
            job.id,
            TrainingStateEvent.model_validate(
                {"kind": "state", "state": "pausing", "reason": "evaluation_pending"}
            ),
            attempt_id=checkpoint.attempt_id,
            fence=checkpoint.fence,
        )
    else:
        trigger.resume_disposition = "operator_override"
    await write_principal_audit(
        session,
        principal=Principal.model_validate(job.authorization_snapshot),
        action="evaluation.training.checkpoint",
        target_type="training_evaluation_trigger",
        target_id=str(trigger.id),
        context={"job_id": job.id, "checkpoint_id": str(checkpoint.id), "schedule_sha256": digest},
    )
    await session.flush()
    return trigger


async def require_checkpoint_pause(
    session: AsyncSession, job: TrainingJobRow, trigger_id: uuid.UUID
) -> TrainingEvaluationTriggerRow:
    """Exact current pause ownership plus all-rank process death, never an asserted pause."""
    from sqlalchemy import select

    from coire_api.db import EvaluationCheckpointPinRow, TrainingAttemptRow

    trigger = await session.get(TrainingEvaluationTriggerRow, trigger_id, populate_existing=True)
    if (
        trigger is None
        or trigger.job_id != job.id
        or trigger.boundary_kind != "checkpoint"
        or trigger.fence != job.fence
        or trigger.pause_version != job.version
        or job.state != "paused"
        or job.pause_origin != "evaluation"
        or job.evaluation_pause_trigger_id != trigger.id
        or job.latest_checkpoint_id != trigger.checkpoint_id
    ):
        raise TrainingConflict("Evaluation no longer owns the exact checkpoint pause")
    active = await session.scalar(
        select(TrainingAttemptRow.id).where(
            TrainingAttemptRow.job_id == job.id,
            TrainingAttemptRow.state.in_(["preparing", "running", "stopping", "unknown"]),
        )
    )
    pin = await session.scalar(
        select(EvaluationCheckpointPinRow.id).where(
            EvaluationCheckpointPinRow.trigger_id == trigger.id,
            EvaluationCheckpointPinRow.checkpoint_id == trigger.checkpoint_id,
            EvaluationCheckpointPinRow.released_at.is_(None),
        )
    )
    checkpoint = await session.get(TrainingCheckpointRow, trigger.checkpoint_id)
    attempt = await session.get(TrainingAttemptRow, checkpoint.attempt_id) if checkpoint else None
    if (
        active is not None
        or pin is None
        or checkpoint is None
        or checkpoint.state != "committed"
        or checkpoint.purged_at is not None
        or attempt is None
        or attempt.state != "stopped"
    ):
        raise TrainingConflict("Evaluation requires retained checkpoint and proven trainer stop")
    from coire_api.db import MemoryReservationRow, NodeRow, TrainingParticipantRow
    from coire_core.models.placement import MemoryReservationState
    from coire_core.models.training_node import TrainingStopReceipt

    participants = (
        await session.scalars(
            select(TrainingParticipantRow).where(TrainingParticipantRow.attempt_id == attempt.id)
        )
    ).all()
    if attempt.stopped_at is None or len(participants) != attempt.world_size:
        raise TrainingConflict("Evaluation awaits all-rank authenticated stop receipts")
    for participant in participants:
        if participant.stop_proof is None or participant.stopped_at is None:
            raise TrainingConflict("Evaluation awaits all-rank authenticated stop receipts")
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
            raise TrainingConflict("Evaluation trainer stop or reservation proof differs")
    return trigger
