"""Audited private adapter curation; training never grants harness verification."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import ModelInstanceRow, ModelRow, ModelVariantRow, TrainingAdapterRow
from coire_api.training.authorization import authorize_live_training_action
from coire_api.training.checkpoints import verified_nodes
from coire_api.training.events import append_event, current_job
from coire_api.training.service import (
    TrainingOperation,
    begin_command,
    recheck_training_base,
    record_receipt,
)
from coire_api.training.telemetry import observed
from coire_core.errors import (
    TrainingConflict,
    TrainingNotFound,
    TrainingQuotaExceeded,
    TrainingUnavailable,
)
from coire_core.models.acquisition import VariantState
from coire_core.models.adapters import (
    AdapterCurationRequest,
    AdapterDetail,
    AdapterReceipt,
    AdapterRetireRequest,
)
from coire_core.models.engine import EngineState, EngineStatus
from coire_core.models.registry import ModelState, Visibility
from coire_core.models.training import (
    CheckpointPromotionRequest,
    ResolvedTrainingSpec,
    TrainingJobState,
    TrainingStateEvent,
)
from coire_core.models.training_node import TrainingArtifactManifest


@observed("coire.api.training.adapter.stage")
async def stage_serving_adapter(
    session: AsyncSession,
    principal: Principal,
    checkpoint_id: uuid.UUID,
    manifest: TrainingArtifactManifest,
    *,
    slug: str,
    automatic: bool = False,
) -> TrainingAdapterRow:
    """Bind an actual node-extracted serving artifact to complete checkpoint lineage.

    This is an internal reducer, not an endpoint accepting administrator evidence.
    The caller must obtain the manifest from the authenticated extraction executor.
    """
    from coire_api.db import TrainingAttemptRow, TrainingCheckpointRow, TrainingJobRow

    await authorize_live_training_action(session, principal)
    checkpoint = await session.get(TrainingCheckpointRow, checkpoint_id)
    if checkpoint is None:
        raise TrainingConflict("Adapter source checkpoint is unavailable")
    job = await session.get(
        TrainingJobRow, checkpoint.job_id, populate_existing=True, with_for_update=True
    )
    if job is None or checkpoint.state != "committed" or job.resolved_spec is None:
        raise TrainingConflict("Adapter requires a complete retained checkpoint")
    resolved = ResolvedTrainingSpec.model_validate(job.resolved_spec)
    await recheck_training_base(session, resolved)
    if (
        manifest.kind != "adapter"
        or manifest.job_id != job.id
        or manifest.attempt_id != checkpoint.attempt_id
        or manifest.fence != checkpoint.fence
        or manifest.update != checkpoint.completed_update
        or manifest.runtime_sha256 != resolved.runtime_sha256
        or manifest.resolved_spec_sha256 != job.resolved_sha256
    ):
        raise TrainingConflict("Serving artifact provenance differs from the checkpoint")
    if await verified_nodes(
        session, checkpoint.id, checkpoint.manifest_sha256, checkpoint.total_bytes
    ) != {"coire-edge-a", "coire-edge-b"}:
        raise TrainingConflict("Source checkpoint does not have both verified copies")
    if automatic:
        principal = Principal.model_validate(job.authorization_snapshot)
        await authorize_live_training_action(session, principal)
        attempt = await session.get(TrainingAttemptRow, checkpoint.attempt_id)
        if (
            job.state not in {"running", "recovering", "finalizing"}
            or checkpoint.fence != job.fence
            or checkpoint.completed_update != resolved.spec.optim.updates
            or slug != job.output_slug
            or attempt is None
            or attempt.state != "stopped"
        ):
            raise TrainingConflict(
                "Automatic output requires completed training and proven trainer termination"
            )
    elif (
        await session.scalar(
            select(TrainingJobRow.id).where(
                TrainingJobRow.model_id == job.model_id, TrainingJobRow.output_slug == slug
            )
        )
        is not None
    ):
        raise TrainingConflict("Promotion name is reserved by a training job")
    existing = await session.get(TrainingAdapterRow, manifest.artifact_id, populate_existing=True)
    if existing is not None:
        if (
            existing.manifest_sha256 is None
            and existing.state == "validating"
            and existing.metadata_record.get("extraction_command_id") is not None
            and existing.slug == slug
            and existing.source_checkpoint_id == checkpoint_id
        ):
            existing.manifest_sha256 = manifest.canonical_sha256()
            existing.metadata_record = {
                **existing.metadata_record,
                "manifest": manifest.model_dump(mode="json"),
            }
            existing.state = "replicating"
            existing.version += 1
            from coire_api.audit import write_principal_audit

            await write_principal_audit(
                session,
                principal=principal,
                action="adapter.extracted",
                target_type="training_adapter",
                target_id=str(existing.id),
                detail={"manifest_sha256": existing.manifest_sha256},
            )
            return existing
        if (
            existing.manifest_sha256 != manifest.canonical_sha256()
            or existing.slug != slug
            or existing.source_checkpoint_id != checkpoint_id
        ):
            raise TrainingConflict("Serving adapter identity is immutable")
        return existing
    if (
        await session.scalar(
            select(TrainingAdapterRow.id).where(
                TrainingAdapterRow.model_id == job.model_id, TrainingAdapterRow.slug == slug
            )
        )
        is not None
    ):
        raise TrainingConflict("Adapter name already exists")
    row = TrainingAdapterRow(
        id=manifest.artifact_id,
        model_id=job.model_id,
        base_variant_id=job.base_variant_id,
        source_job_id=job.id,
        source_checkpoint_id=checkpoint.id,
        slug=slug,
        selector=f"{job.model_id}@{slug}",
        base_manifest_sha256=resolved.base_manifest_sha256,
        manifest_sha256=manifest.canonical_sha256(),
        resolved_spec_sha256=job.resolved_sha256,
        parameterization=resolved.spec.parameterization.kind,
        state="validating",
        visibility="admin_only",
        verified=False,
        version=1,
        metadata_record={
            "manifest": manifest.model_dump(mode="json"),
            "automatic": automatic,
            "authority": principal.model_dump(mode="json"),
        },
    )
    # Validate the strict public identity before storing any candidate.
    AdapterDetail.model_validate(
        {
            "id": row.id,
            "model_id": row.model_id,
            "base_variant_id": row.base_variant_id,
            "slug": slug,
            "selector": row.selector,
            "state": "validating",
            "source_job_id": job.id,
            "source_checkpoint_id": checkpoint.id,
            "base_manifest_sha256": row.base_manifest_sha256,
            "resolved_spec_sha256": row.resolved_spec_sha256,
            "parameterization": row.parameterization,
            "version": 1,
            "created_at": datetime.now(UTC),
        }
    )
    session.add(row)
    if automatic:
        job.state = "finalizing"
        job.version += 1
        job.updated_at = datetime.now(UTC)
        await append_event(
            session, job.id, TrainingStateEvent(kind="state", state=TrainingJobState.FINALIZING)
        )
    from coire_api.audit import write_principal_audit

    await write_principal_audit(
        session,
        principal=principal,
        action="adapter.stage",
        target_type="training_adapter",
        target_id=str(row.id),
        detail={"manifest_sha256": row.manifest_sha256, "source_checkpoint_id": str(checkpoint.id)},
    )
    await session.flush()
    return row


async def promote_checkpoint(
    session: AsyncSession,
    principal: Principal,
    checkpoint_id: uuid.UUID,
    request: CheckpointPromotionRequest,
    manifest: TrainingArtifactManifest,
    key: str,
) -> AdapterReceipt:
    """Complete promotion intent only after authenticated extraction supplied real bytes."""
    from coire_api.db import TrainingCheckpointRow
    from coire_api.training.service import require_job_version

    command = await begin_command(
        session,
        principal,
        operation="checkpoint.promote",
        subject_id=str(checkpoint_id),
        idempotency_key=key,
        request=request,
    )
    if command.receipt is not None:
        return AdapterReceipt.model_validate(command.receipt)
    checkpoint = await session.get(TrainingCheckpointRow, checkpoint_id)
    if checkpoint is None:
        raise TrainingNotFound()
    await require_job_version(session, checkpoint.job_id, request.expected_version)
    row = await stage_serving_adapter(
        session, principal, checkpoint_id, manifest, slug=request.adapter_slug
    )
    command.job_id = checkpoint.job_id
    receipt = AdapterReceipt.model_validate(
        {"adapter_id": row.id, "state": row.state, "version": row.version}
    )
    await record_receipt(session, principal, command, receipt)
    return receipt


async def enqueue_checkpoint_promotion(
    session: AsyncSession,
    principal: Principal,
    checkpoint_id: uuid.UUID,
    request: CheckpointPromotionRequest,
    key: str,
    *,
    extraction_available: bool = False,
) -> AdapterReceipt:
    """Reserve a new private target and persist an executable extraction intent."""
    from sqlalchemy import func

    from coire_api.db import (
        NodeRow,
        TrainingCheckpointRow,
        TrainingCommandRow,
        TrainingJobRow,
        TrainingStorageReservationRow,
    )
    from coire_api.placement.service import lock_nodes_for_admission
    from coire_api.training.service import payload_digest, require_job_version
    from coire_core.models import training_node
    from coire_core.models.node import Reachability

    command = await begin_command(
        session,
        principal,
        operation="checkpoint.promote",
        subject_id=str(checkpoint_id),
        idempotency_key=key,
        request=request,
    )
    if command.receipt is not None:
        return AdapterReceipt.model_validate(command.receipt)
    request_type = getattr(training_node, "TrainingAdapterExtractRequest", None)
    if not extraction_available or request_type is None:
        raise TrainingUnavailable("Reserved node adapter extraction is not available")
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended("coire.training.queue", 0)))
    )
    candidate = await session.get(TrainingCheckpointRow, checkpoint_id)
    if candidate is None:
        raise TrainingNotFound()
    job = await require_job_version(session, candidate.job_id, request.expected_version)
    checkpoint = await session.get(
        TrainingCheckpointRow, checkpoint_id, populate_existing=True, with_for_update=True
    )
    assert checkpoint is not None
    if checkpoint.state != "committed" or job.resolved_spec is None:
        raise TrainingConflict("Promotion requires a complete retained checkpoint")
    resolved = ResolvedTrainingSpec.model_validate(job.resolved_spec)
    await recheck_training_base(session, resolved)
    manifest = TrainingArtifactManifest.model_validate(checkpoint.manifest)
    if manifest.canonical_sha256() != checkpoint.manifest_sha256 or manifest.kind != "checkpoint":
        raise TrainingConflict("Checkpoint manifest identity changed")
    if await verified_nodes(
        session, checkpoint.id, checkpoint.manifest_sha256, checkpoint.total_bytes
    ) != {"coire-edge-a", "coire-edge-b"}:
        raise TrainingConflict("Promotion needs both verified complete checkpoint copies")
    if (
        await session.scalar(
            select(TrainingJobRow.id).where(
                TrainingJobRow.model_id == job.model_id,
                TrainingJobRow.output_slug == request.adapter_slug,
            )
        )
        is not None
        or await session.scalar(
            select(TrainingAdapterRow.id).where(
                TrainingAdapterRow.model_id == job.model_id,
                TrainingAdapterRow.slug == request.adapter_slug,
            )
        )
        is not None
    ):
        raise TrainingConflict("Promotion adapter name is already reserved")
    now = datetime.now(UTC)
    nodes = list(
        (
            await session.scalars(
                select(NodeRow)
                .where(NodeRow.name.in_(["coire-edge-a", "coire-edge-b"]))
                .order_by(NodeRow.name)
            )
        ).all()
    )
    node = next(
        (
            item
            for item in nodes
            if item.reachability is Reachability.HEALTHY
            and item.health_observed_at is not None
            and now - item.health_observed_at <= timedelta(seconds=60)
        ),
        None,
    )
    if node is None:
        raise TrainingUnavailable("No healthy Studio is available for checkpoint extraction")
    await lock_nodes_for_admission(session, [node.id])
    maximum = min(checkpoint.total_bytes + 65536, 20 * 1024**3)
    held = await session.scalar(
        select(func.coalesce(func.sum(TrainingStorageReservationRow.bytes), 0)).where(
            TrainingStorageReservationRow.node_id == node.id,
            TrainingStorageReservationRow.state.in_(["held", "releasing"]),
        )
    )
    if int(held or 0) + maximum * 2 > 200 * 1024**3:
        raise TrainingQuotaExceeded("Adapter extraction storage quota is held")
    adapter_id = uuid.uuid5(command.id, "adapter")
    disk = TrainingStorageReservationRow(
        id=uuid.uuid5(command.id, "disk"),
        owner_user_id=command.actor_user_id,
        node_id=node.id,
        subject_id=str(adapter_id),
        bytes=maximum * 2,
        state="held",
    )
    session.add(disk)
    extraction_id = uuid.uuid5(command.id, "extract")
    extraction = request_type.model_validate(
        {
            "command_id": extraction_id,
            "adapter_id": adapter_id,
            "checkpoint_id": checkpoint.id,
            "checkpoint_manifest_sha256": checkpoint.manifest_sha256,
            "job_id": job.id,
            "attempt_id": checkpoint.attempt_id,
            "fence": checkpoint.fence,
            "node": node.name,
            "resolved": resolved,
            "disk_reservation_id": disk.id,
            "max_bytes": maximum,
            "deadline": now + timedelta(seconds=60),
        }
    )
    row = TrainingAdapterRow(
        id=adapter_id,
        model_id=job.model_id,
        base_variant_id=job.base_variant_id,
        source_job_id=job.id,
        source_checkpoint_id=checkpoint.id,
        slug=request.adapter_slug,
        selector=f"{job.model_id}@{request.adapter_slug}",
        base_manifest_sha256=resolved.base_manifest_sha256,
        resolved_spec_sha256=job.resolved_sha256,
        parameterization=resolved.spec.parameterization.kind,
        state="validating",
        visibility="admin_only",
        verified=False,
        version=1,
        metadata_record={
            "automatic": False,
            "authority": principal.model_dump(mode="json"),
            "extraction_command_id": str(extraction_id),
        },
    )
    session.add(row)
    session.add(
        TrainingCommandRow(
            id=extraction_id,
            actor_user_id=command.actor_user_id,
            idempotency_key=f"adapter-extract:{adapter_id}",
            operation="node.adapter.extract",
            subject_id=node.name,
            job_id=job.id,
            attempt_id=checkpoint.attempt_id,
            request_sha256=payload_digest(extraction),
            payload=extraction.model_dump(mode="json"),
            state="pending",
        )
    )
    job.version += 1
    job.updated_at = now
    command.job_id = job.id
    receipt = AdapterReceipt.model_validate(
        {"adapter_id": adapter_id, "state": "validating", "version": 1}
    )
    await record_receipt(session, principal, command, receipt)
    return receipt


@observed("coire.api.training.adapter.finalize")
async def finalize_serving_adapter(
    session: AsyncSession, adapter_id: uuid.UUID, smoke: EngineStatus, *, instance_id: uuid.UUID
) -> TrainingAdapterRow:
    """Ready only after exact-target reserved node generation and two full copies."""
    from coire_api.db import InstanceMemberRow, MemoryReservationRow
    from coire_core.models.placement import MemoryReservationState, ReservationHolder

    row = await session.get(TrainingAdapterRow, adapter_id)
    if row is None:
        raise TrainingNotFound()
    principal = Principal.model_validate(row.metadata_record.get("authority"))
    await authorize_live_training_action(session, principal)
    job = await current_job(session, row.source_job_id, lock=True)
    await recheck_training_base(session, ResolvedTrainingSpec.model_validate(job.resolved_spec))
    row = await session.get(
        TrainingAdapterRow, adapter_id, populate_existing=True, with_for_update=True
    )
    assert row is not None
    if row.state == "ready":
        return row
    automatic = row.metadata_record.get("automatic") is True
    if row.state not in {"validating", "replicating"} or (automatic and job.state != "finalizing"):
        raise TrainingConflict("Adapter finalization no longer has authority")
    manifest = TrainingArtifactManifest.model_validate(row.metadata_record.get("manifest"))
    if (
        manifest.kind != "adapter"
        or manifest.artifact_id != row.id
        or manifest.canonical_sha256() != row.manifest_sha256
    ):
        raise TrainingConflict("Adapter artifact identity changed")
    if await verified_nodes(
        session, adapter_id, manifest.canonical_sha256(), manifest.total_bytes
    ) != {"coire-edge-a", "coire-edge-b"}:
        raise TrainingConflict("Adapter requires two independently verified copies")
    instance = await session.get(
        ModelInstanceRow, instance_id, populate_existing=True, with_for_update=True
    )
    member = await session.scalar(
        select(InstanceMemberRow).where(
            InstanceMemberRow.instance_id == instance_id,
            InstanceMemberRow.engine_id == smoke.engine_id,
        )
    )
    hold = (
        await session.get(MemoryReservationRow, member.reservation_id)
        if member and member.reservation_id
        else None
    )
    target = smoke.target
    now = datetime.now(UTC)
    if (
        instance is None
        or instance.model_id != row.model_id
        or instance.variant_id != row.base_variant_id
        or instance.adapter_id != row.id
        or member is None
        or hold is None
        or hold.holder_type is not ReservationHolder.MODEL
        or hold.state is not MemoryReservationState.HELD
        or hold.node_id != member.node_id
        or smoke.engine_id is None
        or smoke.state is not EngineState.READY
        or smoke.pid is None
        or smoke.process_create_time is None
        or smoke.last_health_at is None
        or not now - timedelta(seconds=60) <= smoke.last_health_at <= now
        or target is None
        or target.model_id != row.model_id
        or target.variant_id != row.base_variant_id
        or target.adapter_id != row.id
        or target.base_manifest_sha256 != row.base_manifest_sha256
        or target.adapter_manifest_sha256 != row.manifest_sha256
    ):
        raise TrainingConflict("No actual reserved inference smoke for this exact adapter")
    row.state, row.visibility, row.verified = "ready", "admin_only", False
    row.version += 1
    from coire_api.audit import write_principal_audit

    await write_principal_audit(
        session,
        principal=principal,
        action="adapter.ready",
        target_type="training_adapter",
        target_id=str(row.id),
        detail={
            "manifest_sha256": row.manifest_sha256,
            "source_checkpoint_id": str(row.source_checkpoint_id),
        },
    )
    if automatic:
        job.state, job.adapter_id, job.finished_at = "succeeded", row.id, now
        job.version += 1
        await append_event(
            session, job.id, TrainingStateEvent(kind="terminal", state=TrainingJobState.SUCCEEDED)
        )
    return row


def adapter_detail(row: TrainingAdapterRow) -> AdapterDetail:
    return AdapterDetail.model_validate(
        {
            "id": row.id,
            "model_id": row.model_id,
            "base_variant_id": row.base_variant_id,
            "slug": row.slug,
            "selector": row.selector,
            "state": row.state,
            "visibility": row.visibility,
            "manifest_sha256": row.manifest_sha256,
            "base_manifest_sha256": row.base_manifest_sha256,
            "source_job_id": row.source_job_id,
            "source_checkpoint_id": row.source_checkpoint_id,
            "resolved_spec_sha256": row.resolved_spec_sha256,
            "parameterization": row.parameterization,
            "objective": row.objective,
            "verified": row.verified,
            "evaluation_id": row.evaluation_id,
            "version": row.version,
            "created_at": row.created_at,
            "required_entitlements": row.required_entitlements,
        }
    )


@observed("coire.api.training.adapter.curate")
async def curate_adapter(
    session: AsyncSession,
    principal: Principal,
    adapter_id: uuid.UUID,
    request: AdapterCurationRequest | AdapterRetireRequest,
    key: str,
) -> AdapterDetail:
    retiring = isinstance(request, AdapterRetireRequest)
    operation: TrainingOperation = (
        "adapter.retire"
        if retiring
        else (
            "adapter.publish"
            if isinstance(request, AdapterCurationRequest)
            and request.visibility is Visibility.PUBLISHED
            else "adapter.unpublish"
        )
    )
    command = await begin_command(
        session,
        principal,
        operation=operation,
        subject_id=str(adapter_id),
        idempotency_key=key,
        request=request,
    )
    if command.receipt is not None:
        return AdapterDetail.model_validate(command.receipt)
    # Lock base before adapter, matching target resolution and publication authority.
    candidate = await session.get(TrainingAdapterRow, adapter_id)
    if candidate is None:
        raise TrainingNotFound()
    from coire_api.db import TrainingJobRow

    source_job = await session.get(
        TrainingJobRow, candidate.source_job_id, populate_existing=True, with_for_update=True
    )
    model = await session.get(
        ModelRow, candidate.model_id, populate_existing=True, with_for_update=True
    )
    variant = await session.get(
        ModelVariantRow, candidate.base_variant_id, populate_existing=True, with_for_update=True
    )
    row = await session.get(
        TrainingAdapterRow, adapter_id, populate_existing=True, with_for_update=True
    )
    assert row is not None
    if row.version != request.expected_version:
        raise TrainingConflict("Adapter version changed")
    if retiring:
        row.state, row.visibility, row.verified = "retired", "admin_only", False
        # Existing engines retain counted memory and artifact references until stopped.
        # New selection is refused by the central live target resolver.
        from coire_api.instance.service import transition
        from coire_core.models.instance import InstanceState

        instances = list(
            (
                await session.scalars(
                    select(ModelInstanceRow)
                    .where(
                        ModelInstanceRow.adapter_id == row.id,
                        ModelInstanceRow.state == InstanceState.READY,
                    )
                    .order_by(ModelInstanceRow.id)
                )
            ).all()
        )
        for instance in instances:
            await transition(session, instance.id, InstanceState.DRAINING, reason="adapter_retired")
            instance.drain_deadline = datetime.now(UTC) + timedelta(seconds=30)
    else:
        assert isinstance(request, AdapterCurationRequest)
        if request.visibility is Visibility.PUBLISHED:
            if (
                row.state != "ready"
                or row.manifest_sha256 is None
                or model is None
                or variant is None
                or model.state is not ModelState.READY
                or model.visibility is not Visibility.PUBLISHED
                or variant.state is not VariantState.READY
            ):
                raise TrainingConflict(
                    "Publication requires a ready adapter and published ready base"
                )
            row.required_entitlements = sorted(
                set(row.required_entitlements) | set(model.entitlement)
            )
            if source_job is None or source_job.resolved_spec is None:
                raise TrainingConflict("Adapter immutable base lineage is unavailable")
            resolved = ResolvedTrainingSpec.model_validate(source_job.resolved_spec)
            await recheck_training_base(session, resolved)
            if (
                row.base_manifest_sha256 != resolved.base_manifest_sha256
                or row.model_id != resolved.spec.model.model_id
                or row.base_variant_id != resolved.spec.model.variant_id
            ):
                raise TrainingConflict("Adapter base manifest identity changed")
            manifest = TrainingArtifactManifest.model_validate(row.metadata_record.get("manifest"))
            if (
                manifest.kind != "adapter"
                or manifest.artifact_id != row.id
                or manifest.canonical_sha256() != row.manifest_sha256
                or await verified_nodes(
                    session, row.id, manifest.canonical_sha256(), manifest.total_bytes
                )
                != {"coire-edge-a", "coire-edge-b"}
            ):
                raise TrainingConflict("Publication requires both current verified adapter copies")
        if row.state == "retired":
            raise TrainingConflict("Retired adapters cannot be curated")
        row.visibility = request.visibility.value
    row.version += 1
    receipt = adapter_detail(row)
    await record_receipt(session, principal, command, receipt)
    return receipt


async def adapter_is_referenced(session: AsyncSession, adapter_id: uuid.UUID) -> bool:
    from coire_core.models.instance import InstanceState

    return (
        await session.scalar(
            select(ModelInstanceRow.id)
            .where(
                ModelInstanceRow.adapter_id == adapter_id,
                ModelInstanceRow.state.not_in([InstanceState.STOPPED, InstanceState.FAILED]),
            )
            .limit(1)
        )
        is not None
    )
