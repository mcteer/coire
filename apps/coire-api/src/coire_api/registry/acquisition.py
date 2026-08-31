"""Persistence and projections for durable acquisition workflows."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import AcquisitionStageRow, AcquisitionWorkflowRow, ModelRow, ModelVariantRow
from coire_core.models.acquisition import (
    AcquisitionRequest,
    AcquisitionStage,
    AcquisitionState,
    AcquisitionWorkflow,
    ModelVariant,
    Precision,
    StageResult,
    StageStatus,
    VariantRecipe,
    VariantState,
)
from coire_core.models.audit import AuditOutcome
from coire_core.models.registry import ModelState, Visibility, slug_for


class AcquisitionError(RuntimeError):
    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


def variant_slug(base_slug: str, name: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", name.lower()).strip("-.")
    if not normalized:
        raise AcquisitionError(422, "invalid_variant_name", "variant name has no usable characters")
    return f"{base_slug}.{normalized}"


async def submit(
    session: AsyncSession,
    request: AcquisitionRequest,
    *,
    revision: str,
    weight_bytes: int,
    total_bytes: int,
    memory_estimate_bytes: int,
    actor: str,
) -> tuple[ModelRow, ModelVariantRow, AcquisitionWorkflowRow, bool]:
    """Create one workflow or attach to an identical active conversion."""
    model = (
        await session.execute(select(ModelRow).where(ModelRow.repo_id == request.repo_id))
    ).scalar_one_or_none()
    if model is None:
        model = ModelRow(
            id=uuid.uuid4(),
            repo_id=request.repo_id,
            slug=slug_for(request.repo_id),
            display_name=request.repo_id.rsplit("/", 1)[-1],
            state=ModelState.DOWNLOADING,
            visibility=Visibility.ADMIN_ONLY,
            entitlement=[],
            tags=[],
            placement_policy="single:auto",
            precision=request.variant.precision.value,
            weight_bytes=weight_bytes,
            total_bytes=total_bytes,
            file_count=0,
            memory_estimate_bytes=memory_estimate_bytes,
            capability_profile={},
        )
        session.add(model)
        await session.flush()

    existing_variant = (
        await session.execute(
            select(ModelVariantRow).where(
                ModelVariantRow.model_id == model.id,
                ModelVariantRow.name == request.variant.name,
            )
        )
    ).scalar_one_or_none()
    if existing_variant is not None:
        active = (
            await session.execute(
                select(AcquisitionWorkflowRow).where(
                    AcquisitionWorkflowRow.variant_id == existing_variant.id,
                    AcquisitionWorkflowRow.state.in_(
                        [
                            AcquisitionState.QUEUED,
                            AcquisitionState.RUNNING,
                            AcquisitionState.WAITING_FOR_CAPACITY,
                        ]
                    ),
                )
            )
        ).scalar_one_or_none()
        if active is not None and existing_variant.recipe == request.variant.model_dump(
            mode="json"
        ):
            return model, existing_variant, active, False
        raise AcquisitionError(409, "variant_exists", "a variant with this name already exists")

    variant = ModelVariantRow(
        id=uuid.uuid4(),
        model_id=model.id,
        name=request.variant.name,
        slug=variant_slug(model.slug, request.variant.name),
        source_revision=revision,
        precision=request.variant.precision.value,
        recipe=request.variant.model_dump(mode="json"),
        memory_estimate_bytes=memory_estimate_bytes,
        state=VariantState.REQUESTED,
        raw_retained=request.keep_raw,
    )
    workflow = AcquisitionWorkflowRow(
        id=uuid.uuid4(),
        model_id=model.id,
        variant_id=variant.id,
        repo_id=request.repo_id,
        revision=revision,
        request=request.model_dump(mode="json"),
        keep_raw=request.keep_raw,
        stage=AcquisitionStage.INSPECT,
        state=AcquisitionState.QUEUED,
        total_bytes=total_bytes,
    )
    session.add_all([variant, workflow])
    await session.flush()
    await write_audit(
        session,
        actor=actor,
        action="acquisition.submit",
        target_type="acquisition_workflow",
        target_id=str(workflow.id),
        detail={
            "model_id": str(model.id),
            "variant_id": str(variant.id),
            "repo_id": request.repo_id,
        },
    )
    return model, variant, workflow, True


async def reject(
    session: AsyncSession, *, actor: str, repo_id: str, code: str, detail: str
) -> None:
    await write_audit(
        session,
        actor=actor,
        action="acquisition.inspect.refused",
        target_type="huggingface_repo",
        target_id=repo_id,
        outcome=AuditOutcome.REFUSED,
        detail={"code": code, "reason": detail},
    )


async def projection(session: AsyncSession, row: AcquisitionWorkflowRow) -> AcquisitionWorkflow:
    stages = (
        (
            await session.execute(
                select(AcquisitionStageRow)
                .where(AcquisitionStageRow.workflow_id == row.id)
                .order_by(AcquisitionStageRow.started_at)
            )
        )
        .scalars()
        .all()
    )
    return AcquisitionWorkflow(
        id=row.id,
        model_id=row.model_id,
        variant_id=row.variant_id,
        stage=row.stage,
        state=row.state,
        progress_bytes=row.progress_bytes,
        total_bytes=row.total_bytes,
        failure_code=row.failure_code,
        failure_detail=row.failure_detail,
        stages=[
            StageResult(
                stage=item.stage,
                status=item.status,
                attempt=item.attempt,
                public_summary=item.public_summary,
                started_at=item.started_at,
                finished_at=item.finished_at,
            )
            for item in stages
        ],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def retry(session: AsyncSession, row: AcquisitionWorkflowRow, *, actor: str) -> None:
    if row.state is not AcquisitionState.FAILED:
        raise AcquisitionError(409, "not_failed", "only a failed acquisition can be retried")
    successful = set(
        (
            await session.execute(
                select(AcquisitionStageRow.stage).where(
                    AcquisitionStageRow.workflow_id == row.id,
                    AcquisitionStageRow.status == StageStatus.SUCCEEDED,
                )
            )
        ).scalars()
    )
    row.stage = next(
        stage
        for stage in (
            AcquisitionStage.INSPECT,
            AcquisitionStage.PULL,
            AcquisitionStage.CONVERT,
            AcquisitionStage.VALIDATE,
            AcquisitionStage.REPLICATE,
        )
        if stage not in successful
    )
    row.state = AcquisitionState.QUEUED
    row.failure_code = None
    row.failure_detail = None
    row.attempt += 1
    row.updated_at = datetime.now(UTC)
    await write_audit(
        session,
        actor=actor,
        action="acquisition.retry",
        target_type="acquisition_workflow",
        target_id=str(row.id),
        detail={"stage": row.stage.value, "attempt": row.attempt},
    )


def variant_projection(row: ModelVariantRow) -> ModelVariant:
    now = row.created_at
    return ModelVariant(
        id=row.id,
        model_id=row.model_id,
        name=row.name,
        precision=Precision(row.precision),
        recipe=VariantRecipe.model_validate(row.recipe),
        state=row.state,
        byte_size=row.byte_size,
        memory_estimate_bytes=row.memory_estimate_bytes,
        estimate_delta_bytes=row.estimate_delta_bytes,
        validated=row.validated,
        published=row.published,
        is_default=row.is_default,
        raw_retained=row.raw_retained,
        created_at=now,
        updated_at=row.updated_at,
    )
