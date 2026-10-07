"""DBOS-owned durable acquisition stage sequence."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from dbos import DBOS
from opentelemetry import metrics, trace
from sqlalchemy import select

from coire_api.audit import write_audit
from coire_api.db import (
    AcquisitionCommandRow,
    AcquisitionStageRow,
    AcquisitionWorkflowRow,
    InspectionResultRow,
    MemoryReservationRow,
    ModelRow,
    ModelVariantRow,
    NodeMemoryLedgerRow,
    NodeRow,
    ValidationResultRow,
    VariantCopyRow,
    session_scope,
)
from coire_api.placement.service import ensure_ledgers, node_admission_lock
from coire_core.models.acquisition import (
    AcquisitionStage,
    AcquisitionState,
    StageStatus,
    ValidationResult,
    VariantState,
)
from coire_core.models.jobs import JobStatus
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.registry import CopyRole, EngineBackend, ModelState
from coire_core.settings import get_settings

JOB_NAMESPACE = uuid.UUID("5a0d0bf0-0989-4a76-8f2d-2f9a2b66aafe")
tracer = trace.get_tracer("coire.scheduler.acquisition")
meter = metrics.get_meter("coire.scheduler.acquisition")
stage_transitions = meter.create_counter(
    "coire_acquisition_stage_transitions_total",
    unit="1",
    description="Durable acquisition stage transitions.",
)
stage_duration = meter.create_histogram(
    "coire_acquisition_stage_duration_seconds",
    unit="s",
    description="Elapsed acquisition stage duration.",
)
reservation_bytes = meter.create_gauge(
    "coire_acquisition_reservation_bytes", unit="By", description="Held conversion memory."
)
estimate_delta_ratio = meter.create_gauge(
    "coire_acquisition_estimate_delta_ratio",
    unit="1",
    description="Actual converted size delta divided by estimate.",
)
validation_total = meter.create_counter(
    "coire_acquisition_validation_total", unit="1", description="Variant validation outcomes."
)
PHYSICAL_STAGES = (
    AcquisitionStage.PULL,
    AcquisitionStage.CONVERT,
    AcquisitionStage.VALIDATE,
    AcquisitionStage.REPLICATE,
)


def require_validated_backend(result: dict[str, Any], expected: EngineBackend) -> ValidationResult:
    """Keep a text or incomplete visual result from publishing under another backend."""
    validation = ValidationResult.model_validate(result)
    if validation.backend is not expected or not validation.validated:
        raise RuntimeError("node validation did not verify the inspected backend")
    if expected is EngineBackend.MLX_VLM and (
        validation.visual_input is None or not validation.visual_input.verified
    ):
        raise RuntimeError("visual validation has no measured verified capability")
    return validation


def node_job_id(workflow_id: uuid.UUID, stage: AcquisitionStage, attempt: int = 1) -> uuid.UUID:
    return uuid.uuid5(JOB_NAMESPACE, f"{workflow_id}:{stage.value}:{attempt}")


async def _hold_conversion_memory(
    workflow_id: uuid.UUID, node_id: uuid.UUID, memory_bytes: int
) -> None:
    settings = get_settings()
    async with session_scope() as session:
        await ensure_ledgers(
            session,
            budget_bytes=settings.placement_default_budget_bytes,
            sandbox_bytes=settings.placement_sandbox_bytes,
            failover_bytes=settings.failover_reservation_bytes,
        )
        ledger = await session.get(NodeMemoryLedgerRow, node_id)
        if ledger is None:
            raise RuntimeError("conversion node has no memory ledger")
        async with node_admission_lock(session, node_id):
            from coire_api.placement.service import training_allows_work

            if not await training_allows_work(session, [node_id]):
                raise RuntimeError("conversion is waiting for training ownership")
            from coire_api.placement.service import effective_occupied_bytes

            ledger = await session.get(
                NodeMemoryLedgerRow, node_id, populate_existing=True, with_for_update=True
            )
            assert ledger is not None
            reservations = (
                await session.scalars(
                    select(MemoryReservationRow).where(
                        MemoryReservationRow.node_id == node_id,
                        MemoryReservationRow.state.in_(
                            [
                                MemoryReservationState.PENDING,
                                MemoryReservationState.HELD,
                                MemoryReservationState.RELEASING,
                            ]
                        ),
                    )
                )
            ).all()
            occupied = effective_occupied_bytes(reservations, ledger.measured_resident_bytes)
            if occupied is None or occupied + memory_bytes > ledger.budget_bytes:
                raise RuntimeError("conversion is waiting for authoritative memory capacity")
            row = await session.scalar(
                select(MemoryReservationRow).where(
                    MemoryReservationRow.node_id == node_id,
                    MemoryReservationRow.holder_type == ReservationHolder.CONVERSION,
                    MemoryReservationRow.holder_id == str(workflow_id),
                )
            )
            if row is None:
                session.add(
                    MemoryReservationRow(
                        node_id=node_id,
                        holder_type=ReservationHolder.CONVERSION,
                        holder_id=str(workflow_id),
                        bytes=memory_bytes,
                        state=MemoryReservationState.HELD,
                    )
                )
            else:
                row.bytes = memory_bytes
                row.state = MemoryReservationState.HELD
                row.released_at = None


async def _release_conversion_memory(workflow_id: uuid.UUID, node_id: uuid.UUID) -> None:
    async with session_scope() as session:
        row = await session.scalar(
            select(MemoryReservationRow).where(
                MemoryReservationRow.node_id == node_id,
                MemoryReservationRow.holder_type == ReservationHolder.CONVERSION,
                MemoryReservationRow.holder_id == str(workflow_id),
            )
        )
        if row is not None:
            row.state = MemoryReservationState.RELEASED
            row.released_at = datetime.now(UTC)


async def _context(workflow_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as session:
        workflow = await session.get(AcquisitionWorkflowRow, workflow_id)
        if workflow is None:
            raise RuntimeError("acquisition workflow no longer exists")
        variant = await session.get(ModelVariantRow, workflow.variant_id)
        model = await session.get(ModelRow, workflow.model_id)
        origin = await session.get(NodeRow, workflow.origin_node_id)
        replica = await session.get(NodeRow, workflow.replica_node_id)
        inspection = await session.get(InspectionResultRow, workflow.id)
        if (
            variant is None
            or model is None
            or origin is None
            or replica is None
            or inspection is None
        ):
            raise RuntimeError("acquisition workflow references incomplete state")
        return {
            "workflow_id": workflow.id,
            "model_id": workflow.model_id,
            "variant_id": workflow.variant_id,
            "repo_id": workflow.repo_id,
            "revision": workflow.revision,
            "keep_raw": workflow.keep_raw,
            "origin": origin.name,
            "replica": replica.name,
            "origin_id": origin.id,
            "replica_id": replica.id,
            "model_slug": model.slug,
            "variant_slug": variant.slug,
            "recipe": dict(variant.recipe),
            "memory_estimate_bytes": variant.memory_estimate_bytes,
            "total_bytes": workflow.total_bytes,
            "attempt": workflow.attempt,
            "inspection": dict(inspection.result),
            "source_variant_slug": workflow.request.get("source_variant_slug"),
        }


async def _start_stage(workflow_id: uuid.UUID, stage: AcquisitionStage) -> None:
    stage_transitions.add(1, {"stage": stage.value, "outcome": "started"})
    async with session_scope() as session:
        workflow = await session.get(AcquisitionWorkflowRow, workflow_id)
        variant = await session.get(ModelVariantRow, workflow.variant_id) if workflow else None
        if workflow is None or variant is None:
            raise RuntimeError("acquisition workflow disappeared")
        existing = (
            await session.execute(
                select(AcquisitionStageRow).where(
                    AcquisitionStageRow.workflow_id == workflow_id,
                    AcquisitionStageRow.stage == stage,
                    AcquisitionStageRow.attempt == workflow.attempt,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                AcquisitionStageRow(
                    id=uuid.uuid4(),
                    workflow_id=workflow_id,
                    stage=stage,
                    status=StageStatus.RUNNING,
                    attempt=workflow.attempt,
                    node_job_id=node_job_id(workflow_id, stage, workflow.attempt),
                    started_at=datetime.now(UTC),
                )
            )
        workflow.stage = stage
        workflow.state = AcquisitionState.RUNNING
        workflow.updated_at = datetime.now(UTC)
        variant.state = {
            AcquisitionStage.PULL: VariantState.PULLING,
            AcquisitionStage.CONVERT: VariantState.CONVERTING,
            AcquisitionStage.VALIDATE: VariantState.VALIDATING,
            AcquisitionStage.REPLICATE: VariantState.REPLICATING,
        }[stage]
        await write_audit(
            session,
            actor="coire-scheduler",
            action=f"acquisition.{stage.value}.start",
            target_type="acquisition_workflow",
            target_id=str(workflow_id),
        )


async def _finish_stage(
    workflow_id: uuid.UUID,
    stage: AcquisitionStage,
    *,
    result: dict[str, Any] | None = None,
    summary: str,
) -> None:
    async with session_scope() as session:
        workflow = await session.get(AcquisitionWorkflowRow, workflow_id)
        if workflow is None:
            raise RuntimeError("acquisition workflow disappeared")
        row = (
            await session.execute(
                select(AcquisitionStageRow).where(
                    AcquisitionStageRow.workflow_id == workflow_id,
                    AcquisitionStageRow.stage == stage,
                    AcquisitionStageRow.attempt == workflow.attempt,
                )
            )
        ).scalar_one()
        row.status = StageStatus.SUCCEEDED
        row.result = result
        row.public_summary = summary
        row.finished_at = datetime.now(UTC)
        if row.started_at is not None:
            stage_duration.record(
                max(0.0, (row.finished_at - row.started_at).total_seconds()),
                {"stage": stage.value, "outcome": "succeeded"},
            )
        stage_transitions.add(1, {"stage": stage.value, "outcome": "succeeded"})
        await write_audit(
            session,
            actor="coire-scheduler",
            action=f"acquisition.{stage.value}.succeeded",
            target_type="acquisition_workflow",
            target_id=str(workflow_id),
            detail={"summary": summary},
        )


async def _reference_perplexity(
    model_id: uuid.UUID, variant_id: uuid.UUID
) -> tuple[uuid.UUID | None, float | None]:
    async with session_scope() as session:
        row = (
            (
                await session.execute(
                    select(ValidationResultRow)
                    .join(ModelVariantRow, ModelVariantRow.id == ValidationResultRow.variant_id)
                    .where(
                        ModelVariantRow.model_id == model_id,
                        ModelVariantRow.id != variant_id,
                        ValidationResultRow.validated.is_(True),
                    )
                    .order_by(ModelVariantRow.is_default.desc(), ValidationResultRow.created_at)
                )
            )
            .scalars()
            .first()
        )
        value = row.result.get("perplexity") if row is not None else None
        if row is None or not isinstance(value, int | float):
            return None, None
        return row.variant_id, float(value)


async def _successful_stage_job_id(workflow_id: uuid.UUID, stage: AcquisitionStage) -> uuid.UUID:
    async with session_scope() as session:
        row = (
            (
                await session.execute(
                    select(AcquisitionStageRow)
                    .where(
                        AcquisitionStageRow.workflow_id == workflow_id,
                        AcquisitionStageRow.stage == stage,
                        AcquisitionStageRow.status == StageStatus.SUCCEEDED,
                    )
                    .order_by(AcquisitionStageRow.attempt.desc())
                )
            )
            .scalars()
            .first()
        )
        if row is None or row.node_job_id is None:
            raise RuntimeError(f"successful {stage.value} stage has no node job id")
        return row.node_job_id


def replication_source_stage(
    source_is_mlx: bool, source_variant_slug: str | None
) -> AcquisitionStage:
    return (
        AcquisitionStage.PULL
        if source_is_mlx and not source_variant_slug
        else AcquisitionStage.CONVERT
    )


async def _submit_command(
    workflow_id: uuid.UUID,
    stage: AcquisitionStage,
    *,
    node_id: uuid.UUID,
    operation: str,
    payload: dict[str, object],
    attempt: int,
) -> dict[str, object]:
    command_id = node_job_id(workflow_id, stage, attempt)
    async with session_scope() as session:
        row = await session.get(AcquisitionCommandRow, command_id)
        if row is None:
            session.add(
                AcquisitionCommandRow(
                    id=command_id,
                    workflow_id=workflow_id,
                    stage=stage,
                    node_id=node_id,
                    operation=operation,
                    payload=payload,
                    state="pending",
                )
            )
    while True:
        async with session_scope() as session:
            row = await session.get(AcquisitionCommandRow, command_id)
            if row is None:
                raise RuntimeError("acquisition command disappeared")
            if row.state == "succeeded":
                return dict(row.result or {})
            if row.state == "failed":
                raise RuntimeError(f"node command failed: {row.failure_code or 'unknown'}")
        await asyncio.sleep(get_settings().acquisition_poll_interval_s)


async def _run_command_stage(
    workflow_id: uuid.UUID,
    stage: AcquisitionStage,
    context: dict[str, Any],
    source_is_mlx: bool,
    raw_slug: str,
) -> None:
    settings = get_settings()
    if stage is AcquisitionStage.PULL:
        target = context["variant_slug"] if source_is_mlx else raw_slug
        status = JobStatus.model_validate(
            await _submit_command(
                workflow_id,
                stage,
                node_id=context["origin_id"],
                operation="pull",
                attempt=context["attempt"],
                payload={
                    "repo_id": context["repo_id"],
                    "slug": target,
                    "revision": context["revision"],
                    "total_bytes": context["total_bytes"],
                },
            )
        )
        await _finish_stage(
            workflow_id,
            stage,
            result={"manifest_sha256": status.manifest_sha256, "source_is_mlx": source_is_mlx},
            summary="source pulled once and checksum verified",
        )
        return
    if stage is AcquisitionStage.CONVERT:
        # An existing verified MLX variant is a source, not the new target.
        # It still needs the explicit dequantize/convert stage to materialize
        # the requested variant slug before validation and replication.
        if source_is_mlx and not context.get("source_variant_slug"):
            await _finish_stage(
                workflow_id, stage, result={"operation": "noop"}, summary="source is already MLX"
            )
            return
        reservation_bytes.set(context["memory_estimate_bytes"], {"node": context["origin"]})
        await _hold_conversion_memory(
            workflow_id,
            context["origin_id"],
            max(1, context["memory_estimate_bytes"]),
        )
        try:
            status = JobStatus.model_validate(
                await _submit_command(
                    workflow_id,
                    stage,
                    node_id=context["origin_id"],
                    operation="convert",
                    attempt=context["attempt"],
                    payload={
                        "workflow_id": str(workflow_id),
                        "variant_id": str(context["variant_id"]),
                        "memory_bytes": max(1, context["memory_estimate_bytes"]),
                        "disk_bytes": max(1, context["total_bytes"]),
                        "repo_id": context["repo_id"],
                        "revision": context["revision"],
                        "source_slug": context["source_variant_slug"] or raw_slug,
                        "target_slug": context["variant_slug"],
                        "recipe": context["recipe"],
                        "dequantize": bool(context["source_variant_slug"]),
                    },
                )
            )
        finally:
            await _release_conversion_memory(workflow_id, context["origin_id"])
            reservation_bytes.set(0, {"node": context["origin"]})
        await _finish_stage(
            workflow_id,
            stage,
            result={"manifest_sha256": status.manifest_sha256},
            summary="MLX conversion completed atomically",
        )
        return
    if stage is AcquisitionStage.VALIDATE:
        backend = EngineBackend(context["inspection"].get("backend", EngineBackend.MLX_LM))
        reference_id, reference = (
            (None, None)
            if backend is EngineBackend.MLX_VLM
            else await _reference_perplexity(context["model_id"], context["variant_id"])
        )
        status = JobStatus.model_validate(
            await _submit_command(
                workflow_id,
                stage,
                node_id=context["origin_id"],
                operation="validate",
                attempt=context["attempt"],
                payload={
                    "slug": context["variant_slug"],
                    "backend": backend.value,
                    "tolerance": settings.acquisition_perplexity_tolerance,
                    "validator_version": settings.acquisition_validation_fixture_version,
                    "chat_template_present": bool(
                        context["inspection"].get("chat_template_present", False)
                    ),
                    "reference_perplexity": reference,
                    "reference_variant_id": str(reference_id) if reference_id else None,
                },
            )
        )
        result = dict(status.result or {})
        validation = require_validated_backend(result, backend)
        validation_total.add(1, {"outcome": "pass", "backend": backend.value})
        async with session_scope() as session:
            validation_row = (
                await session.execute(
                    select(ValidationResultRow).where(
                        ValidationResultRow.workflow_id == workflow_id
                    )
                )
            ).scalar_one_or_none()
            if validation_row is None:
                session.add(
                    ValidationResultRow(
                        id=uuid.uuid4(),
                        workflow_id=workflow_id,
                        variant_id=context["variant_id"],
                        result=result,
                        validated=validation.validated,
                    )
                )
            else:
                validation_row.result = result
                validation_row.validated = validation.validated
            variant = await session.get(ModelVariantRow, context["variant_id"])
            if variant is not None:
                variant.validated = validation.validated
        await _finish_stage(workflow_id, stage, result=result, summary="validation checks recorded")
        return
    if stage is AcquisitionStage.REPLICATE:
        source_stage = replication_source_stage(source_is_mlx, context.get("source_variant_slug"))
        source_job = await _successful_stage_job_id(workflow_id, source_stage)
        command = await _submit_command(
            workflow_id,
            stage,
            node_id=context["replica_id"],
            operation="replicate",
            attempt=context["attempt"],
            payload={
                "origin": context["origin"],
                "source_job_id": str(source_job),
                "slug": context["variant_slug"],
            },
        )
        source = JobStatus.model_validate(command["source"])
        replica = JobStatus.model_validate(command["replica"])
        if source.manifest_sha256 != replica.manifest_sha256:
            raise RuntimeError("replica manifest differs from origin")
        await _record_ready(context, source, replica)
        await _finish_stage(
            workflow_id,
            stage,
            result={"manifest_sha256": source.manifest_sha256, "copies": 2},
            summary="two verified copies share one manifest",
        )


@DBOS.step(retries_allowed=True, max_attempts=5, interval_seconds=2.0)
async def run_stage(workflow_id_text: str, stage_text: str) -> None:
    workflow_id = uuid.UUID(workflow_id_text)
    stage = AcquisitionStage(stage_text)
    with tracer.start_as_current_span(f"coire.scheduler.acquisition.{stage.value}") as span:
        span.set_attribute("coire.workflow_id", workflow_id_text)
        span.set_attribute("coire.stage", stage_text)
        context = await _context(workflow_id)
    inspection = context["inspection"]
    source_is_mlx = inspection.get("source_format") == "mlx"
    raw_slug = f"{context['model_slug']}.raw"
    await _start_stage(workflow_id, stage)
    await _run_command_stage(workflow_id, stage, context, source_is_mlx, raw_slug)
    return


async def _stage_result(workflow_id: uuid.UUID, stage: AcquisitionStage) -> dict[str, Any]:
    async with session_scope() as session:
        row = (
            (
                await session.execute(
                    select(AcquisitionStageRow)
                    .where(
                        AcquisitionStageRow.workflow_id == workflow_id,
                        AcquisitionStageRow.stage == stage,
                        AcquisitionStageRow.status == StageStatus.SUCCEEDED,
                    )
                    .order_by(AcquisitionStageRow.attempt.desc())
                )
            )
            .scalars()
            .first()
        )
        return dict(row.result or {}) if row is not None else {}


@DBOS.step(retries_allowed=True, max_attempts=3)
async def stage_complete(workflow_id_text: str, stage_text: str) -> bool:
    workflow_id = uuid.UUID(workflow_id_text)
    stage = AcquisitionStage(stage_text)
    async with session_scope() as session:
        workflow = await session.get(AcquisitionWorkflowRow, workflow_id)
        if workflow is None:
            raise RuntimeError("acquisition workflow disappeared")
        results = list(
            (
                await session.execute(
                    select(AcquisitionStageRow.result).where(
                        AcquisitionStageRow.workflow_id == workflow_id,
                        AcquisitionStageRow.stage == stage,
                        AcquisitionStageRow.status.in_(
                            [StageStatus.SUCCEEDED, StageStatus.SKIPPED]
                        ),
                    )
                )
            ).scalars()
        )
        source_variant_slug = workflow.request.get("source_variant_slug")
        return reusable_stage_results(
            stage, source_variant_slug if isinstance(source_variant_slug, str) else None, results
        )


def reusable_stage_results(
    stage: AcquisitionStage, source_variant_slug: str | None, results: list[dict[str, Any] | None]
) -> bool:
    """A stale MLX no-op cannot stand in for a missing derived variant."""
    if stage is AcquisitionStage.CONVERT and source_variant_slug:
        return any(result is not None and bool(result.get("manifest_sha256")) for result in results)
    return bool(results)


async def _record_ready(context: dict[str, Any], origin: JobStatus, replica: JobStatus) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        variant = await session.get(ModelVariantRow, context["variant_id"])
        model = await session.get(ModelRow, context["model_id"])
        workflow = await session.get(AcquisitionWorkflowRow, context["workflow_id"])
        if variant is None or model is None or workflow is None or origin.manifest is None:
            raise RuntimeError("cannot publish incomplete acquisition state")
        backend = EngineBackend(context["inspection"].get("backend", EngineBackend.MLX_LM))
        validation_row = (
            await session.execute(
                select(ValidationResultRow).where(
                    ValidationResultRow.workflow_id == context["workflow_id"]
                )
            )
        ).scalar_one_or_none()
        if validation_row is None or not validation_row.validated or not variant.validated:
            raise RuntimeError("cannot publish a variant without passing validation")
        validation = require_validated_backend(validation_row.result, backend)
        if origin.manifest_sha256 is None or origin.manifest_sha256 != replica.manifest_sha256:
            raise RuntimeError("cannot publish mismatched variant copies")
        for node_id, role, status in (
            (context["origin_id"], CopyRole.ORIGIN, origin),
            (context["replica_id"], CopyRole.REPLICA, replica),
        ):
            copy = (
                await session.execute(
                    select(VariantCopyRow).where(
                        VariantCopyRow.variant_id == variant.id,
                        VariantCopyRow.node_id == node_id,
                    )
                )
            ).scalar_one_or_none()
            if copy is None:
                copy = VariantCopyRow(
                    id=uuid.uuid4(),
                    variant_id=variant.id,
                    node_id=node_id,
                )
                session.add(copy)
            copy.path = context["variant_slug"]
            copy.bytes = origin.manifest.total_bytes
            copy.manifest_sha256 = status.manifest_sha256
            copy.verified = True
            copy.verified_at = now
            copy.role = role
        variant.byte_size = origin.manifest.total_bytes
        variant.backend = backend.value
        variant.visual_capability = (
            validation.visual_input.model_dump(mode="json") if validation.visual_input else None
        )
        variant.estimate_delta_bytes = origin.manifest.total_bytes - variant.memory_estimate_bytes
        if variant.memory_estimate_bytes:
            estimate_delta_ratio.set(variant.estimate_delta_bytes / variant.memory_estimate_bytes)
        variant.state = VariantState.READY
        model.state = ModelState.READY
        model.backend = backend.value
        model.visual_capability = variant.visual_capability
        model.capability_profile = {
            **(model.capability_profile or {}),
            **({"visual_input": variant.visual_capability} if variant.visual_capability else {}),
        }
        model.precision = variant.precision
        model.total_bytes = origin.manifest.total_bytes
        model.manifest_sha256 = origin.manifest_sha256
        model.ready_at = now


@DBOS.step(retries_allowed=True, max_attempts=3)
async def mark_failed(workflow_id_text: str, stage_text: str, detail: str) -> None:
    workflow_id = uuid.UUID(workflow_id_text)
    stage = AcquisitionStage(stage_text)
    safe_detail = detail[:500]
    stage_transitions.add(1, {"stage": stage.value, "outcome": "failed"})
    async with session_scope() as session:
        workflow = await session.get(AcquisitionWorkflowRow, workflow_id)
        variant = await session.get(ModelVariantRow, workflow.variant_id) if workflow else None
        if workflow is None or variant is None:
            return
        row = (
            await session.execute(
                select(AcquisitionStageRow).where(
                    AcquisitionStageRow.workflow_id == workflow_id,
                    AcquisitionStageRow.stage == stage,
                    AcquisitionStageRow.attempt == workflow.attempt,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.status = StageStatus.FAILED
            row.failure_code = "stage_failed"
            row.public_summary = safe_detail
            row.finished_at = datetime.now(UTC)
        workflow.stage = AcquisitionStage.FAILED
        workflow.state = AcquisitionState.FAILED
        workflow.failure_code = f"{stage.value}_failed"
        workflow.failure_detail = safe_detail
        workflow.updated_at = datetime.now(UTC)
        variant.state = VariantState.FAILED
        await write_audit(
            session,
            actor="coire-scheduler",
            action=f"acquisition.{stage.value}.failed",
            target_type="acquisition_workflow",
            target_id=workflow_id_text,
            detail={"reason": safe_detail},
        )


@DBOS.workflow(name="coire.acquisition.workflow", max_recovery_attempts=100)
async def acquisition_workflow(workflow_id_text: str) -> None:
    for stage in PHYSICAL_STAGES:
        if await stage_complete(workflow_id_text, stage.value):
            continue
        try:
            await run_stage(workflow_id_text, stage.value)
        except Exception as exc:
            await mark_failed(
                workflow_id_text,
                stage.value,
                f"{type(exc).__name__}: stage failed after retry exhaustion",
            )
            raise
    await cleanup_raw(workflow_id_text)


@DBOS.step(retries_allowed=True, max_attempts=5, interval_seconds=2.0)
async def cleanup_raw(workflow_id_text: str) -> None:
    workflow_id = uuid.UUID(workflow_id_text)
    context = await _context(workflow_id)
    if not context["keep_raw"] and context["inspection"].get("source_format") != "mlx":
        await _submit_command(
            workflow_id,
            AcquisitionStage.DONE,
            node_id=context["origin_id"],
            operation="cleanup",
            attempt=context["attempt"],
            payload={"slug": f"{context['model_slug']}.raw"},
        )
    async with session_scope() as session:
        workflow = await session.get(AcquisitionWorkflowRow, context["workflow_id"])
        variant = await session.get(ModelVariantRow, context["variant_id"])
        if workflow is None or variant is None:
            raise RuntimeError("acquisition disappeared before finalization")
        workflow.stage = AcquisitionStage.DONE
        workflow.state = AcquisitionState.SUCCEEDED
        workflow.updated_at = datetime.now(UTC)
        variant.raw_retained = bool(context["keep_raw"])
        await write_audit(
            session,
            actor="coire-scheduler",
            action="acquisition.done",
            target_type="acquisition_workflow",
            target_id=workflow_id_text,
            detail={"raw_retained": variant.raw_retained},
        )
