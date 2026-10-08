"""Short audited, idempotent transactions shared by training management operations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from opentelemetry import metrics, trace
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    ModelRow,
    ModelVariantRow,
    NodeRow,
    TrainingAttemptRow,
    TrainingCommandRow,
    TrainingDatasetAnalysisRow,
    TrainingDatasetReferenceRow,
    TrainingDatasetRevisionRow,
    TrainingJobRow,
    VariantCopyRow,
)
from coire_api.training.authorization import authorize_live_training_action
from coire_api.training.events import append_event
from coire_api.training.specs import parse_submission
from coire_core.errors import (
    TrainingConflict,
    TrainingNotFound,
    TrainingQuotaExceeded,
    TrainingValidationError,
)
from coire_core.models.acquisition import VariantState
from coire_core.models.datasets import DatasetAnalysis, DatasetAnalysisBinding, SplitManifest
from coire_core.models.registry import ModelState
from coire_core.models.training import (
    TERMINAL_TRAINING_STATES,
    ResolvedTrainingSpecDocument,
    TrainingCommandReceipt,
    TrainingControlRequest,
    TrainingJobDetail,
    TrainingJobReceipt,
    TrainingJobState,
    TrainingStateEvent,
    TrainingSubmission,
    parse_resolved_training_spec,
)
from coire_core.models.training_node import TrainingPrepareRequest
from coire_core.settings import Settings, get_settings


def training_id() -> str:
    """A time-ordered ULID without introducing a runtime dependency."""
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "".join(alphabet[(value >> (5 * index)) & 31] for index in reversed(range(26)))


def encode_page_cursor(scope: str, created_at: datetime, identity: str) -> str:
    """Opaque, versioned bounded read position; it confers no authority."""
    value = [1, scope, created_at.isoformat(), identity, int(time.time()) + 900]
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def decode_page_cursor(cursor: str, scope: str) -> tuple[datetime, str]:
    try:
        if len(cursor) > 512:
            raise ValueError()
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(value, list)
            or len(value) != 5
            or type(value[0]) is not int
            or value[0] != 1
            or value[1] != scope
            or type(value[4]) is not int
            or not int(time.time()) < value[4] <= int(time.time()) + 900
            or not isinstance(value[2], str)
            or not isinstance(value[3], str)
        ):
            raise ValueError()
        timestamp = datetime.fromisoformat(value[2])
        if timestamp.tzinfo is None:
            raise ValueError()
        identity = value[3]
        if not 1 <= len(identity) <= 128:
            raise ValueError()
        return timestamp, identity
    except (ValueError, TypeError, binascii.Error, UnicodeError):
        raise TrainingValidationError("Invalid, expired or foreign page cursor") from None


async def recheck_training_base(
    session: AsyncSession, resolved: ResolvedTrainingSpecDocument
) -> None:
    """Recheck mutable authority over immutable inputs at every execution boundary."""
    model = await session.get(
        ModelRow, resolved.spec.model.model_id, populate_existing=True, with_for_update=True
    )
    variant = await session.get(
        ModelVariantRow,
        resolved.spec.model.variant_id,
        populate_existing=True,
        with_for_update=True,
    )
    if (
        model is None
        or variant is None
        or variant.model_id != model.id
        or model.state is not ModelState.READY
        or variant.state is not VariantState.READY
        or model.source != "studio"
        or model.kind != "language_model"
        or variant.backend != "mlx_lm"
        or model.backend != "mlx_lm"
    ):
        raise TrainingConflict("Training requires an acquired ready local text variant")
    copies = await session.execute(
        select(NodeRow.name, VariantCopyRow.manifest_sha256)
        .join(VariantCopyRow, VariantCopyRow.node_id == NodeRow.id)
        .where(VariantCopyRow.variant_id == variant.id, VariantCopyRow.verified.is_(True))
    )
    if {name for name, digest in copies if digest == resolved.base_manifest_sha256} != {
        "coire-edge-a",
        "coire-edge-b",
    }:
        raise TrainingConflict("Immutable base copies changed")


async def recheck_training_inputs(
    session: AsyncSession, resolved: ResolvedTrainingSpecDocument
) -> None:
    await recheck_training_base(session, resolved)
    train_hashes: set[str] = set()
    validation_hashes: set[str] = set()
    for binding in sorted(resolved.datasets, key=lambda item: str(item.dataset_id)):
        source = await session.get(
            TrainingDatasetRevisionRow,
            binding.dataset_id,
            populate_existing=True,
            with_for_update=True,
        )
        analysis = await session.get(
            TrainingDatasetAnalysisRow,
            binding.analysis_id,
            populate_existing=True,
            with_for_update=True,
        )
        if (
            source is None
            or source.state != "ready"
            or source.purged_at is not None
            or source.source_sha256 != binding.source_sha256
            or source.split_sha256 != binding.split_sha256
            or analysis is None
            or analysis.dataset_id != binding.dataset_id
            or analysis.state != "succeeded"
            or analysis.model_id != resolved.spec.model.model_id
            or analysis.variant_id != resolved.spec.model.variant_id
            or analysis.result is None
        ):
            raise TrainingConflict("Pinned input or analysis is unavailable")
        try:
            result = DatasetAnalysis.model_validate(analysis.result)
            split = SplitManifest.model_validate(source.split_manifest)
        except ValueError:
            raise TrainingConflict("Pinned input evidence is invalid") from None
        if (
            payload_digest(result) != binding.analysis_sha256
            or result.tokenizer_sha256 != resolved.tokenizer_sha256
            or result.template_sha256 != resolved.template_sha256
            or result.runtime_sha256 != resolved.runtime_sha256
            or result.tokens is None
            or result.tokens.maximum > resolved.spec.optim.max_sequence_length
            or split.dataset_id != binding.dataset_id
            or split.source_sha256 != binding.source_sha256
        ):
            raise TrainingConflict("Training analysis does not match immutable execution bounds")
        for source_spec in resolved.spec.data.train.datasets:
            if source_spec.dataset_id == binding.dataset_id:
                if source_spec.sample_count > len(split.train_rows):
                    raise TrainingConflict(
                        "Selected training sample pool exceeds its immutable split"
                    )
                train_hashes.update(split.row_content_sha256[row - 1] for row in split.train_rows)
        if binding.dataset_id in resolved.spec.data.validation.dataset_ids:
            validation_hashes.update(
                split.row_content_sha256[row - 1] for row in split.validation_rows
            )
    if train_hashes & validation_hashes:
        raise TrainingConflict("Duplicate content crosses training and validation sources")


async def frozen_training_inputs(
    session: AsyncSession, prepare: TrainingPrepareRequest
) -> tuple[DatasetAnalysisBinding, SplitManifest, DatasetAnalysis]:
    """Return the existing strict metadata models for node journal.stage_inputs()."""
    from coire_api.db import TrainingParticipantRow

    job = await session.get(TrainingJobRow, prepare.job_id, populate_existing=True)
    if job is None:
        raise TrainingNotFound()
    await authorize_live_training_action(
        session, Principal.model_validate(job.authorization_snapshot)
    )
    job = await session.get(
        TrainingJobRow, prepare.job_id, populate_existing=True, with_for_update=True
    )
    assert job is not None
    attempt = await session.get(TrainingAttemptRow, prepare.attempt_id, populate_existing=True)
    if (
        job.fence != prepare.fence
        or attempt is None
        or attempt.fence != prepare.fence
        or attempt.job_id != job.id
        or attempt.state not in {"preparing", "running"}
        or job.state not in {"reserving", "running"}
        or prepare.lease_expires_at <= datetime.now(UTC)
        or payload_digest(prepare.resolved) != job.resolved_sha256
        or prepare.world_size != 1
        or len(prepare.resolved.datasets) != 1
    ):
        raise TrainingConflict("Input staging no longer belongs to the authorized attempt")
    participant = await session.scalar(
        select(TrainingParticipantRow.id)
        .join(NodeRow, NodeRow.id == TrainingParticipantRow.node_id)
        .where(
            TrainingParticipantRow.attempt_id == attempt.id,
            NodeRow.name == prepare.node,
            TrainingParticipantRow.reservation_id == prepare.reservation_id,
            TrainingParticipantRow.rank == prepare.rank,
        )
    )
    if participant is None:
        raise TrainingConflict("Input staging participant identity changed")
    persisted = await session.get(TrainingCommandRow, prepare.command_id)
    if (
        persisted is None
        or persisted.operation != "node.training.prepare"
        or persisted.payload != prepare.model_dump(mode="json")
        or persisted.request_sha256 != payload_digest(prepare)
    ):
        raise TrainingConflict("Input staging request differs from persisted preparation")
    await recheck_training_inputs(session, prepare.resolved)
    selected = prepare.resolved.datasets[0]
    source = await session.get(TrainingDatasetRevisionRow, selected.dataset_id)
    analysis_row = await session.get(TrainingDatasetAnalysisRow, selected.analysis_id)
    assert source is not None and analysis_row is not None
    command = await session.get(TrainingCommandRow, analysis_row.command_id)
    if command is None:
        raise TrainingConflict("Frozen analysis binding is unavailable")
    binding = DatasetAnalysisBinding.model_validate(command.payload.get("analysis"))
    split = SplitManifest.model_validate(source.split_manifest)
    analysis = DatasetAnalysis.model_validate(analysis_row.result)
    if (
        payload_digest(binding) != analysis_row.identity_sha256
        or binding.base_manifest_sha256 != prepare.resolved.base_manifest_sha256
        or binding.source_sha256 != selected.source_sha256
        or binding.split_sha256 != selected.split_sha256
        or binding.dataset_id != selected.dataset_id
        or binding.model_id != job.model_id
        or binding.variant_id != job.base_variant_id
        or binding.enable_thinking != prepare.resolved.enable_thinking
    ):
        raise TrainingConflict("Frozen input binding differs from immutable execution intent")
    return binding, split, analysis


async def job_detail(session: AsyncSession, job_id: str) -> TrainingJobDetail:
    job = await session.get(TrainingJobRow, job_id, populate_existing=True)
    if job is None:
        raise TrainingNotFound()
    if job.deleted_at is not None:
        from coire_api.db import TrainingAdapterRow

        if (
            await session.scalar(
                select(TrainingAdapterRow.id)
                .where(TrainingAdapterRow.source_job_id == job_id)
                .limit(1)
            )
            is None
        ):
            raise TrainingNotFound()
    attempt = await session.scalar(
        select(TrainingAttemptRow).where(
            TrainingAttemptRow.job_id == job.id, TrainingAttemptRow.fence == job.fence
        )
    )
    from coire_api.evaluation.links import for_job

    return TrainingJobDetail.model_validate(
        {
            "id": job.id,
            "evaluation_groups": await for_job(session, job.id),
            "version": job.version,
            "state": job.state,
            "source_yaml": job.source_yaml,
            "source_sha256": job.source_sha256,
            "intent_sha256": job.intent_sha256,
            "spec": job.submitted_spec,
            "resolved": job.resolved_spec,
            "attempt_id": attempt.id if attempt else None,
            "completed_update": job.completed_update,
            "latest_checkpoint_id": job.latest_checkpoint_id,
            "adapter_id": job.adapter_id,
            "reason": job.safe_reason,
            "reproducible": job.reproducible,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }
    )


async def submit_training(
    session: AsyncSession,
    principal: Principal,
    submission: TrainingSubmission,
    key: str,
    *,
    resolved: ResolvedTrainingSpecDocument | None = None,
    global_limit: int = 8,
    owner_limit: int = 4,
    settings: Settings | None = None,
) -> TrainingJobReceipt:
    """Commit immutable, already-measured intent and input pins atomically.

    The resolver is trusted control-plane code, never a caller-supplied estimate.
    Dataset retirement takes the same source row locks as this transaction.
    """
    config = settings or get_settings()
    parsed = parse_submission(submission, settings=config)
    command = await begin_command(
        session,
        principal,
        operation="training.submit",
        subject_id=f"{parsed.spec.model.model_id}@{parsed.spec.output.adapter_slug}",
        idempotency_key=key,
        request=submission,
        intent_sha256=parsed.intent_sha256,
    )
    if command.receipt is not None:
        return TrainingJobReceipt.model_validate(command.receipt)
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended("coire.training.queue", 0)))
    )
    if resolved is None:
        from coire_api.training.specs import resolve_submission
        from coire_core.errors import TrainingUnavailable

        validation = await resolve_submission(
            session, submission, settings=config, principal=principal
        )
        if not validation.ready_to_run or validation.resolved is None:
            raise TrainingUnavailable(
                "Training preflight is unavailable: " + ",".join(validation.reasons)
            )
        resolved = validation.resolved
    if resolved.spec != parsed.spec:
        raise TrainingConflict("Resolved settings differ from submitted intent")
    if submission.preview_sha256 is not None and submission.preview_sha256 != payload_digest(
        resolved
    ):
        raise TrainingConflict("Resolved preview changed; validate the recipe again")
    active = select(TrainingJobRow).where(
        TrainingJobRow.state.not_in([str(s) for s in TERMINAL_TRAINING_STATES])
    )
    jobs = list((await session.scalars(active)).all())
    if len(jobs) >= min(global_limit, config.training_max_pending_global) or sum(
        j.owner_user_id == command.actor_user_id for j in jobs
    ) >= min(owner_limit, config.training_max_pending_per_admin):
        raise TrainingQuotaExceeded("Training queue limit reached")
    existing = await session.scalar(
        select(TrainingJobRow.id).where(
            TrainingJobRow.model_id == parsed.spec.model.model_id,
            TrainingJobRow.output_slug == parsed.spec.output.adapter_slug,
        )
    )
    if existing is not None:
        raise TrainingConflict("Output adapter name is already reserved")
    from coire_api.db import TrainingAdapterRow

    if (
        await session.scalar(
            select(TrainingAdapterRow.id).where(
                TrainingAdapterRow.model_id == parsed.spec.model.model_id,
                TrainingAdapterRow.slug == parsed.spec.output.adapter_slug,
            )
        )
        is not None
    ):
        raise TrainingConflict("Output adapter name is already reserved")
    await recheck_training_inputs(session, resolved)
    required = {s.dataset_id for s in parsed.spec.data.train.datasets} | set(
        parsed.spec.data.validation.dataset_ids
    )
    bindings = {b.dataset_id: b for b in resolved.datasets}
    if set(bindings) != required or len(bindings) != len(resolved.datasets):
        raise TrainingValidationError("Resolved inputs do not exactly cover the recipe")
    for dataset_id in sorted(required, key=str):
        source = await session.get(
            TrainingDatasetRevisionRow, dataset_id, populate_existing=True, with_for_update=True
        )
        binding = bindings[dataset_id]
        analysis = await session.get(
            TrainingDatasetAnalysisRow,
            binding.analysis_id,
            populate_existing=True,
            with_for_update=True,
        )
        if (
            source is None
            or source.state != "ready"
            or source.purged_at is not None
            or source.source_sha256 != binding.source_sha256
            or source.split_sha256 != binding.split_sha256
            or analysis is None
            or analysis.dataset_id != dataset_id
            or analysis.state != "succeeded"
            or analysis.model_id != parsed.spec.model.model_id
            or analysis.variant_id != parsed.spec.model.variant_id
            or analysis.tokenizer_sha256 != resolved.tokenizer_sha256
            or analysis.template_sha256 != resolved.template_sha256
            or analysis.runtime_sha256 != resolved.runtime_sha256
        ):
            raise TrainingConflict("Training input is unavailable or its analysis binding changed")
    now = datetime.now(UTC)
    job = TrainingJobRow(
        id=training_id(),
        owner_user_id=command.actor_user_id,
        originating_key_id=principal.api_key_id,
        originating_key_version=principal.credential_version,
        model_id=parsed.spec.model.model_id,
        base_variant_id=parsed.spec.model.variant_id,
        idempotency_key=key,
        output_slug=parsed.spec.output.adapter_slug,
        source_yaml=parsed.source_yaml,
        source_sha256=parsed.source_sha256,
        intent_sha256=parsed.intent_sha256,
        submitted_spec=parsed.spec.model_dump(mode="json"),
        resolved_spec=resolved.model_dump(mode="json"),
        resolved_sha256=payload_digest(resolved),
        authorization_snapshot=principal.model_dump(mode="json"),
        state="queued",
        version=1,
        fence=0,
        completed_update=0,
        queue_deadline_at=now + timedelta(seconds=config.training_queue_timeout_s),
        execution_deadline_at=now
        + timedelta(seconds=config.training_queue_timeout_s + config.training_execution_timeout_s),
    )
    session.add(job)
    await session.flush()
    for binding in resolved.datasets:
        session.add(
            TrainingDatasetReferenceRow(
                job_id=job.id,
                dataset_id=binding.dataset_id,
                analysis_id=binding.analysis_id,
                source_sha256=binding.source_sha256,
                split_sha256=binding.split_sha256,
            )
        )
    command.job_id = job.id
    receipt = TrainingJobReceipt(
        job_id=job.id,
        version=job.version,
        state=TrainingJobState.QUEUED,
        events_path=f"/api/v1/admin/training/jobs/{job.id}/events",
    )
    await append_event(
        session, job.id, TrainingStateEvent(kind="state", state=TrainingJobState.QUEUED)
    )
    await record_receipt(session, principal, command, receipt)
    return receipt


async def control_training(
    session: AsyncSession,
    principal: Principal,
    job_id: str,
    operation: Literal["pause", "resume", "cancel"],
    request: TrainingControlRequest,
    key: str,
) -> TrainingCommandReceipt:
    # Cross-administrator resume checks acquire both identity rows in one order.
    # This avoids actor-A/origin-B and actor-B/origin-A lock inversion.
    initial = await session.get(TrainingJobRow, job_id)
    if operation == "resume" and initial is not None:
        from coire_api.db import ApiKeyRow, UserRow

        users = {initial.owner_user_id}
        if principal.user_id is not None:
            users.add(principal.user_id)
        for identity in sorted(users, key=str):
            await session.get(UserRow, identity, populate_existing=True, with_for_update=True)
        keys = {
            identity
            for identity in (initial.originating_key_id, principal.api_key_id)
            if identity is not None
        }
        for identity in sorted(keys, key=str):
            await session.get(ApiKeyRow, identity, populate_existing=True, with_for_update=True)
    operations: dict[str, TrainingOperation] = {
        "pause": "training.pause",
        "resume": "training.resume",
        "cancel": "training.cancel",
    }
    command = await begin_command(
        session,
        principal,
        operation=operations[operation],
        subject_id=job_id,
        idempotency_key=key,
        request=request,
    )
    if command.receipt is not None:
        return TrainingCommandReceipt.model_validate(command.receipt)
    job = await require_job_version(session, job_id, request.expected_version)
    state = TrainingJobState(job.state)
    if state in TERMINAL_TRAINING_STATES:
        raise TrainingConflict("Terminal training jobs cannot be controlled")
    if operation == "resume":
        if state is not TrainingJobState.PAUSED:
            raise TrainingConflict("Only a paused job may resume")
        if job.evaluation_pause_trigger_id is not None:
            from coire_api.db import TrainingEvaluationTriggerRow

            obligation = await session.get(
                TrainingEvaluationTriggerRow, job.evaluation_pause_trigger_id
            )
            if obligation is not None and obligation.phase != "complete":
                raise TrainingConflict(
                    "Evaluation cleanup must complete before training can resume"
                )
            job.evaluation_pause_trigger_id = None
        await authorize_live_training_action(
            session, Principal.model_validate(job.authorization_snapshot)
        )
        attempt = await session.scalar(
            select(TrainingAttemptRow).where(
                TrainingAttemptRow.job_id == job.id,
                TrainingAttemptRow.state.in_(["preparing", "running", "stopping", "unknown"]),
            )
        )
        if attempt is not None:
            raise TrainingConflict("Old trainers have not been proved stopped")
        if job.resolved_spec is None:
            raise TrainingConflict("Resolved training inputs are missing")
        await recheck_training_inputs(session, parse_resolved_training_spec(job.resolved_spec))
        from coire_api.training.checkpoints import recovery_checkpoint

        checkpoint = await recovery_checkpoint(session, job)
        if job.completed_update and checkpoint is None:
            raise TrainingConflict("No valid complete checkpoint remains for resume")
        job.latest_checkpoint_id = checkpoint.id if checkpoint else None
        job.completed_update = checkpoint.completed_update if checkpoint else 0
        job.state, job.pause_origin, job.safe_reason = "queued", None, None
        config = get_settings()
        remaining = config.training_execution_timeout_s - job.cumulative_execution_seconds
        if remaining <= 0:
            raise TrainingConflict("Cumulative execution deadline has been exhausted")
        job.queue_deadline_at = datetime.now(UTC) + timedelta(
            seconds=config.training_queue_timeout_s
        )
        job.execution_deadline_at = job.queue_deadline_at + timedelta(seconds=remaining)
    elif operation == "pause":
        overriding_evaluation = job.pause_origin == "evaluation" and state in {
            TrainingJobState.PAUSING,
            TrainingJobState.PAUSED,
        }
        if (
            state not in {TrainingJobState.RUNNING, TrainingJobState.QUEUED}
            and not overriding_evaluation
        ):
            raise TrainingConflict("Job cannot pause in its current state")
        job.state = (
            "paused" if state in {TrainingJobState.QUEUED, TrainingJobState.PAUSED} else "pausing"
        )
        job.pause_origin, job.safe_reason = "admin", "admin_pause"
    else:
        active = await session.scalar(
            select(TrainingAttemptRow.id).where(
                TrainingAttemptRow.job_id == job.id,
                TrainingAttemptRow.state.in_(["preparing", "running", "stopping", "unknown"]),
            )
        )
        job.state = "cancelled" if active is None else "cancelling"
        if active is None:
            job.finished_at = datetime.now(UTC)
        job.safe_reason = "cancelled"
    job.version += 1
    job.updated_at = datetime.now(UTC)
    if job.state in {"pausing", "cancelling"}:
        active_attempt = await session.scalar(
            select(TrainingAttemptRow.id).where(
                TrainingAttemptRow.job_id == job.id,
                TrainingAttemptRow.state.in_(["preparing", "running", "stopping", "unknown"]),
            )
        )
        if active_attempt is not None:
            from coire_scheduler.training import enqueue_controls

            await enqueue_controls(
                session, active_attempt, "pause" if job.state == "pausing" else "stop"
            )
    command.job_id = job.id
    receipt = TrainingCommandReceipt(
        command_id=command.id, job_id=job.id, state=TrainingJobState(job.state), version=job.version
    )
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
    await record_receipt(session, principal, command, receipt)
    return receipt


type TrainingOperation = Literal[
    "dataset.upload",
    "dataset.analyze",
    "dataset.delete",
    "training.submit",
    "training.pause",
    "training.resume",
    "training.cancel",
    "training.delete",
    "training.measure",
    "checkpoint.promote",
    "adapter.publish",
    "adapter.unpublish",
    "adapter.retire",
]
tracer = trace.get_tracer("coire.api.training")
commands = metrics.get_meter("coire.api.training").create_counter(
    "coire_training_commands_total", unit="{command}"
)


def payload_digest(request: BaseModel) -> str:
    if request.model_config.get("extra") != "forbid":
        raise TypeError("training commands require a strict wire model")
    payload = json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


async def begin_command(
    session: AsyncSession,
    principal: Principal,
    *,
    operation: TrainingOperation,
    subject_id: str,
    idempotency_key: str,
    request: BaseModel,
    intent_sha256: str | None = None,
) -> TrainingCommandRow:
    """Return prior intent or persist a new command inside the caller's transaction.

    Submission supplies its parsed canonical intent hash, so comments or defaults cannot
    accidentally redefine a retry. A command with a stored receipt is replayed before
    checking a newer resource version; no implicit second mutation is authorized.
    """
    owner = await authorize_live_training_action(session, principal)
    if (
        not 1 <= len(idempotency_key) <= 128
        or len(idempotency_key.encode("utf-8")) > 512
        or any(ord(char) < 32 for char in idempotency_key)
    ):
        raise TrainingValidationError("Idempotency key must be a bounded opaque value")
    if not subject_id or len(subject_id) > 128:
        raise TrainingValidationError("Invalid training command subject")
    request_hash = intent_sha256 or payload_digest(request)
    if len(request_hash) != 64 or any(char not in "0123456789abcdef" for char in request_hash):
        raise TrainingValidationError("Invalid canonical intent digest")
    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    with tracer.start_as_current_span("coire.api.training.command") as span:
        span.set_attribute("coire.operation", operation)
        lock_key = f"coire.training.command:{owner}:{operation}:{key_hash}"
        await session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )
        prior = await session.scalar(
            select(TrainingCommandRow)
            .where(
                TrainingCommandRow.actor_user_id == owner,
                TrainingCommandRow.idempotency_key == idempotency_key,
                TrainingCommandRow.operation == operation,
            )
            .with_for_update()
        )
        if prior is not None:
            if prior.subject_id != subject_id or prior.request_sha256 != request_hash:
                raise TrainingConflict("Idempotency key already identifies different intent")
            commands.add(1, {"operation": operation, "outcome": "replay"})
            return prior
        command = TrainingCommandRow(
            id=uuid.uuid4(),
            actor_user_id=owner,
            idempotency_key=idempotency_key,
            operation=operation,
            subject_id=subject_id,
            request_sha256=request_hash,
            payload=request.model_dump(mode="json"),
            state="pending",
        )
        session.add(command)
        await session.flush()
        commands.add(1, {"operation": operation, "outcome": "created"})
        return command


async def record_receipt(
    session: AsyncSession, principal: Principal, command: TrainingCommandRow, receipt: BaseModel
) -> None:
    """The receipt and mandatory audit commit atomically with their domain mutation."""
    owner = await authorize_live_training_action(session, principal)
    if command.actor_user_id != owner:
        raise TrainingConflict("Training command belongs to another actor")
    if receipt.model_config.get("extra") != "forbid":
        raise TypeError("training receipt must be a strict wire model")
    value = receipt.model_dump(mode="json")
    if command.receipt is not None:
        if command.receipt != value:
            raise TrainingConflict("Completed command receipt is immutable")
        return
    command.receipt = value
    command.state = "succeeded"
    await write_principal_audit(
        session,
        principal=principal,
        action=command.operation,
        target_type="training_resource",
        target_id=command.subject_id,
        detail={"command_id": str(command.id), "intent_sha256": command.request_sha256},
    )
    await session.flush()


async def require_job_version(
    session: AsyncSession, job_id: str, expected_version: int
) -> TrainingJobRow:
    job = await session.get(TrainingJobRow, job_id, populate_existing=True, with_for_update=True)
    if job is None or job.deleted_at is not None:
        raise TrainingNotFound()
    if job.version != expected_version:
        raise TrainingConflict("Training job version changed; reload its current state")
    return job
