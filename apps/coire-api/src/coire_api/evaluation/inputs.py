"""Freeze metadata and mint exact live Studio grants; core never scans suite inputs."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    EvaluationAttemptRow,
    EvaluationRunRow,
    NodeRow,
    TrainingCheckpointRow,
    TrainingCommandRow,
    TrainingDatasetRevisionRow,
)
from coire_api.evaluation.authorization import authorize_live_evaluation_action
from coire_api.evaluation.telemetry import observed
from coire_api.training.service import payload_digest
from coire_core.errors import TrainingNotFound
from coire_core.models.datasets import DatasetFormat, SplitManifest
from coire_core.models.evaluation import (
    EvaluationInputFile,
    EvaluationTarget,
    EvaluationWorkload,
    canonical_digest,
)
from coire_core.models.evaluation_inputs import (
    EvaluationDatasetGrant,
    EvaluationTrainingBinding,
    EvaluationTrainingInputsRequest,
    EvaluationTrainingSource,
)
from coire_core.models.training import parse_resolved_training_spec
from coire_core.models.training_node import TrainingArtifactManifest
from coire_core.settings import Settings
from coire_core.training_data import split_digest


def split_bytes(split: SplitManifest) -> bytes:
    return json.dumps(
        split.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


@observed("coire.api.evaluation.inputs.freeze")
async def freeze_training_inputs(
    session: AsyncSession, run: EvaluationRunRow
) -> tuple[EvaluationTrainingBinding | None, list[EvaluationInputFile]]:
    snapshot = run.data_snapshot
    if snapshot is None:
        return None, []
    resolved = parse_resolved_training_spec(snapshot["resolved_spec"])
    if payload_digest(resolved) != snapshot["resolved_sha256"]:
        return None, []
    checkpoint = await session.get(TrainingCheckpointRow, uuid.UUID(str(snapshot["checkpoint_id"])))
    if (
        checkpoint is None
        or checkpoint.state != "committed"
        or checkpoint.job_id != snapshot["job_id"]
        or checkpoint.completed_update != snapshot["completed_update"]
    ):
        return None, []
    manifest = TrainingArtifactManifest.model_validate(checkpoint.manifest)
    if (
        manifest.canonical_sha256() != checkpoint.manifest_sha256
        or manifest.resolved_spec_sha256 != snapshot["resolved_sha256"]
    ):
        return None, []
    rank = next((item for item in manifest.ranks if item.rank == 0), None)
    state = next((item for item in manifest.files if rank and item.id == rank.state_file_id), None)
    if state is None or state.bytes > 8 * 1024**2 or manifest.runtime_sha256 is None:
        return None, []
    frozen = {item.dataset_id: item for item in resolved.datasets}
    sources = []
    for item in resolved.spec.data.train.datasets:
        revision = await session.get(TrainingDatasetRevisionRow, item.dataset_id)
        identity = frozen.get(item.dataset_id)
        if (
            revision is None
            or revision.state != "ready"
            or identity is None
            or revision.source_sha256 != identity.source_sha256
            or revision.split_sha256 != identity.split_sha256
        ):
            return None, []
        split = SplitManifest.model_validate(revision.split_manifest)
        if split_digest(split) != identity.split_sha256:
            return None, []
        sources.append(
            EvaluationTrainingSource(
                dataset_id=revision.id,
                format=DatasetFormat(revision.format),
                source_sha256=identity.source_sha256,
                source_bytes=revision.source_bytes,
                split_sha256=identity.split_sha256,
                split_bytes=len(split_bytes(split)),
            )
        )
    binding = EvaluationTrainingBinding(
        job_id=checkpoint.job_id,
        training_attempt_id=checkpoint.attempt_id,
        training_fence=checkpoint.fence,
        checkpoint_id=checkpoint.id,
        checkpoint_manifest_sha256=checkpoint.manifest_sha256,
        resolved_spec_sha256=str(snapshot["resolved_sha256"]),
        runtime_sha256=manifest.runtime_sha256,
        completed_update=checkpoint.completed_update,
        batch_size=resolved.spec.optim.batch_size,
        accumulation_steps=resolved.spec.optim.accumulation_steps,
        seed=resolved.spec.data.train.seed,
        mixture=resolved.spec.data.train,
        state_file_id=state.id,
        state_sha256=state.sha256,
        state_bytes=state.bytes,
        sources=sources,
    )
    files = [
        EvaluationInputFile(
            name="training-state.json",
            sha256=state.sha256,
            bytes=state.bytes,
            purpose="training_state",
        )
    ]
    for source in sources:
        files.extend(
            [
                EvaluationInputFile(
                    name=f"source-{source.dataset_id}.jsonl",
                    sha256=source.source_sha256,
                    bytes=source.source_bytes,
                    purpose="training_source",
                ),
                EvaluationInputFile(
                    name=f"split-{source.dataset_id}.json",
                    sha256=source.split_sha256,
                    bytes=source.split_bytes,
                    purpose="split_manifest",
                ),
            ]
        )
    return binding, files


@observed("coire.api.evaluation.inputs.grant")
async def mint_inputs(
    session: AsyncSession, workload: EvaluationWorkload, node: str, settings: Settings
) -> EvaluationTrainingInputsRequest:
    run = await session.get(EvaluationRunRow, workload.evaluation_id)
    attempt = await session.get(EvaluationAttemptRow, workload.attempt_id)
    if (
        run is None
        or attempt is None
        or run.fence != workload.fence
        or run.state in {"cancelling", "succeeded", "failed", "cancelled", "timed_out"}
        or attempt.workload != workload.model_dump(mode="json")
        or attempt.agent_run_id != workload.run_id
    ):
        raise TrainingNotFound()
    await authorize_live_evaluation_action(
        session, Principal.model_validate(run.authorization_snapshot)
    )
    run = await session.get(
        EvaluationRunRow, workload.evaluation_id, populate_existing=True, with_for_update=True
    )
    attempt = await session.get(EvaluationAttemptRow, workload.attempt_id, populate_existing=True)
    actual_node = await session.get(NodeRow, attempt.node_id) if attempt else None
    if (
        run is None
        or attempt is None
        or actual_node is None
        or actual_node.name != node
        or run.fence != workload.fence
        or run.state in {"cancelling", "succeeded", "failed", "cancelled", "timed_out"}
        or attempt.state in {"stopping", "released"}
        or attempt.run_id != run.id
        or attempt.agent_run_id != workload.run_id
    ):
        raise TrainingNotFound()
    binding, _files = await freeze_training_inputs(session, run)
    if binding is None or binding != workload.training or workload.deadline <= datetime.now(UTC):
        raise TrainingNotFound()
    grants = []
    for source in binding.sources:
        secret = secrets.token_urlsafe(32)
        identity = uuid.uuid4()
        expiry = min(
            workload.deadline,
            datetime.now(UTC) + timedelta(seconds=settings.training_transfer_grant_s),
        )
        grant = EvaluationDatasetGrant.model_validate(
            {
                "grant_id": identity,
                "evaluation_attempt_id": attempt.id,
                "dataset_id": source.dataset_id,
                "node": node,
                "source_sha256": source.source_sha256,
                "max_bytes": source.source_bytes,
                "expires_at": expiry,
                "secret": secret,
            }
        )
        session.add(
            TrainingCommandRow(
                id=identity,
                actor_user_id=run.owner_user_id,
                idempotency_key=f"evaluation-input-{identity}",
                operation="evaluation.input.grant",
                subject_id=str(source.dataset_id),
                request_sha256=hashlib.sha256(secret.encode()).hexdigest(),
                job_id=binding.job_id,
                payload={
                    "grant": grant.model_dump(mode="json", exclude={"secret"}),
                    "run_id": run.id,
                    "fence": run.fence,
                    "workload_sha256": canonical_digest(workload),
                },
                state="succeeded",
            )
        )
        grants.append(grant)
    from coire_api.audit import write_principal_audit

    await write_principal_audit(
        session,
        principal=Principal.model_validate(run.authorization_snapshot),
        action="evaluation.input.grant",
        target_type="evaluation_attempt",
        target_id=str(attempt.id),
        context={
            "run_id": run.id,
            "node": node,
            "source_count": len(grants),
            "checkpoint_id": str(binding.checkpoint_id),
        },
    )
    await session.flush()
    return EvaluationTrainingInputsRequest(
        run_id=workload.run_id, request_sha256=canonical_digest(workload), grants=grants
    )


async def authorized_source(
    session: AsyncSession, dataset_id: uuid.UUID, node: str, secret: str
) -> TrainingDatasetRevisionRow:
    digest = hashlib.sha256(secret.encode()).hexdigest()
    command = await session.scalar(
        select(TrainingCommandRow).where(
            TrainingCommandRow.operation == "evaluation.input.grant",
            TrainingCommandRow.request_sha256 == digest,
        )
    )
    if command is None or not hmac.compare_digest(command.request_sha256, digest):
        raise TrainingNotFound()
    metadata = command.payload.get("grant")
    if not isinstance(metadata, dict):
        raise TrainingNotFound()
    grant = EvaluationDatasetGrant.model_validate({**metadata, "secret": secret})
    run = await session.get(EvaluationRunRow, str(command.payload["run_id"]))
    attempt = await session.get(EvaluationAttemptRow, grant.evaluation_attempt_id)
    if (
        grant.dataset_id != dataset_id
        or grant.node != node
        or grant.expires_at <= datetime.now(UTC)
        or run is None
        or attempt is None
        or run.fence != command.payload["fence"]
        or run.state in {"cancelling", "succeeded", "failed", "cancelled", "timed_out"}
        or attempt.state in {"stopping", "released"}
    ):
        raise TrainingNotFound()
    await authorize_live_evaluation_action(
        session, Principal.model_validate(run.authorization_snapshot)
    )
    run = await session.get(EvaluationRunRow, run.id, populate_existing=True, with_for_update=True)
    attempt = await session.get(
        EvaluationAttemptRow, grant.evaluation_attempt_id, populate_existing=True
    )
    actual_node = await session.get(NodeRow, attempt.node_id) if attempt else None
    if (
        run is None
        or attempt is None
        or actual_node is None
        or actual_node.name != node
        or run.fence != command.payload["fence"]
        or run.state in {"cancelling", "succeeded", "failed", "cancelled", "timed_out"}
        or attempt.state in {"stopping", "released"}
        or attempt.run_id != run.id
        or attempt.fence != run.fence
        or command.actor_user_id != run.owner_user_id
        or command.state != "succeeded"
    ):
        raise TrainingNotFound()
    workload = EvaluationWorkload.model_validate(attempt.workload)
    if (
        canonical_digest(workload) != command.payload["workload_sha256"]
        or workload.training is None
        or workload.deadline <= datetime.now(UTC)
    ):
        raise TrainingNotFound()
    source = next(
        (item for item in workload.training.sources if item.dataset_id == dataset_id), None
    )
    revision = await session.get(TrainingDatasetRevisionRow, dataset_id)
    if (
        source is None
        or revision is None
        or revision.state != "ready"
        or revision.source_sha256 != source.source_sha256
        or revision.source_bytes != source.source_bytes
        or grant.source_sha256 != source.source_sha256
        or grant.max_bytes != source.source_bytes
    ):
        raise TrainingNotFound()
    return revision


async def split_input(session: AsyncSession, workload: EvaluationWorkload, name: str) -> bytes:
    if workload.training is None:
        raise TrainingNotFound()
    source = next(
        (item for item in workload.training.sources if name == f"split-{item.dataset_id}.json"),
        None,
    )
    if source is None:
        raise TrainingNotFound()
    revision = await session.get(TrainingDatasetRevisionRow, source.dataset_id)
    if (
        revision is None
        or revision.state != "ready"
        or revision.split_sha256 != source.split_sha256
    ):
        raise TrainingNotFound()
    raw = split_bytes(SplitManifest.model_validate(revision.split_manifest))
    if len(raw) != source.split_bytes or hashlib.sha256(raw).hexdigest() != source.split_sha256:
        raise TrainingNotFound()
    return raw


@observed("coire.api.evaluation.inputs.lineage")
async def manual_training_context(
    session: AsyncSession,
    job_id: str,
    subjects: list[EvaluationTarget],
    *,
    checkpoint_measurement: bool = False,
) -> tuple[dict[str, object], TrainingCheckpointRow]:
    """Explicit context can refer only to the exact completed base/final adapter."""
    from coire_api.db import TrainingAdapterRow, TrainingJobRow
    from coire_core.errors import EvaluationValidationError

    job = await session.get(TrainingJobRow, job_id, populate_existing=True, with_for_update=True)
    if checkpoint_measurement and job is not None and job.evaluation_pause_trigger_id is not None:
        from coire_api.evaluation.training import require_checkpoint_pause
        from coire_core.models.training import ResolvedTrainingSpecV2

        trigger = await require_checkpoint_pause(session, job, job.evaluation_pause_trigger_id)
        adapter = await session.get(
            TrainingAdapterRow, uuid.uuid5(trigger.id, "checkpoint-adapter")
        )
        checkpoint = await session.get(TrainingCheckpointRow, trigger.checkpoint_id)
        resolved = parse_resolved_training_spec(job.resolved_spec)
        if (
            adapter is None
            or adapter.state != "ready"
            or checkpoint is None
            or not isinstance(resolved, ResolvedTrainingSpecV2)
            or payload_digest(resolved) != job.resolved_sha256
            or any(
                subject.target.model_id != resolved.spec.model.model_id
                or subject.target.variant_id != resolved.spec.model.variant_id
                or subject.target.base_manifest_sha256 != resolved.base_manifest_sha256
                or subject.target.adapter_id not in {None, adapter.id}
                or (
                    subject.target.adapter_id == adapter.id
                    and subject.target.adapter_manifest_sha256 != adapter.manifest_sha256
                )
                for subject in subjects
            )
        ):
            raise EvaluationValidationError(
                "Checkpoint measurement differs from its frozen subjects"
            )
        return {
            "job_id": job.id,
            "checkpoint_id": str(checkpoint.id),
            "completed_update": checkpoint.completed_update,
            "resolved_spec": resolved.model_dump(mode="json"),
            "resolved_sha256": job.resolved_sha256,
        }, checkpoint
    if (
        job is None
        or job.state != "succeeded"
        or job.deleted_at is not None
        or job.resolved_spec is None
        or job.adapter_id is None
    ):
        raise EvaluationValidationError("Training input context requires a completed training job")
    resolved = parse_resolved_training_spec(job.resolved_spec)
    adapter = await session.get(TrainingAdapterRow, job.adapter_id)
    checkpoint = (
        await session.get(TrainingCheckpointRow, adapter.source_checkpoint_id)
        if adapter and adapter.source_checkpoint_id
        else None
    )
    if (
        payload_digest(resolved) != job.resolved_sha256
        or adapter is None
        or adapter.state != "ready"
        or adapter.source_job_id != job.id
        or adapter.metadata_record.get("automatic") is not True
        or checkpoint is None
        or checkpoint.job_id != job.id
        or checkpoint.completed_update != resolved.spec.optim.updates
        or job.completed_update != checkpoint.completed_update
        or checkpoint.fence != job.fence
        or any(
            subject.target.model_id != resolved.spec.model.model_id
            or subject.target.variant_id != resolved.spec.model.variant_id
            or subject.target.base_manifest_sha256 != resolved.base_manifest_sha256
            or subject.target.adapter_id not in {None, adapter.id}
            for subject in subjects
        )
    ):
        raise EvaluationValidationError(
            "Training input context differs from exact completed subjects"
        )
    return {
        "job_id": job.id,
        "checkpoint_id": str(checkpoint.id),
        "completed_update": checkpoint.completed_update,
        "resolved_spec": resolved.model_dump(mode="json"),
        "resolved_sha256": job.resolved_sha256,
    }, checkpoint
