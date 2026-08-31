"""Variant comparison and safe publication state transitions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import ModelRow, ModelVariantRow, ValidationResultRow
from coire_api.registry.acquisition import AcquisitionError, variant_projection
from coire_core.models.acquisition import ModelVariant, ValidationResult, VariantPublication
from coire_core.models.audit import AuditOutcome
from coire_core.models.registry import Visibility


async def list_variants(session: AsyncSession, model_id: uuid.UUID) -> list[ModelVariant]:
    rows = list(
        (
            await session.execute(
                select(ModelVariantRow)
                .where(ModelVariantRow.model_id == model_id)
                .order_by(ModelVariantRow.created_at)
            )
        )
        .scalars()
        .all()
    )
    results = {
        result.variant_id: result
        for result in (
            await session.execute(
                select(ValidationResultRow).where(
                    ValidationResultRow.variant_id.in_([row.id for row in rows])
                )
            )
        )
        .scalars()
        .all()
    }
    output: list[ModelVariant] = []
    for row in rows:
        item = variant_projection(row)
        stored = results.get(row.id)
        if stored is not None:
            item.validation = ValidationResult.model_validate(stored.result)
        output.append(item)
    return output


async def update_publication(
    session: AsyncSession,
    row: ModelVariantRow,
    request: VariantPublication,
    *,
    actor: str,
) -> ModelVariant:
    if (request.published is True or request.is_default is True) and not (
        row.state.value == "ready" and row.validated
    ):
        await write_audit(
            session,
            actor=actor,
            action="variant.publish",
            target_type="model_variant",
            target_id=str(row.id),
            outcome=AuditOutcome.REFUSED,
            detail={"reason": "variant must be ready and validated"},
        )
        raise AcquisitionError(
            409, "variant_not_validated", "only a ready validated variant can be published"
        )
    if request.is_default is True:
        await session.execute(
            update(ModelVariantRow)
            .where(ModelVariantRow.model_id == row.model_id, ModelVariantRow.id != row.id)
            .values(is_default=False)
        )
        row.is_default = True
        row.published = True
        model = await session.get(ModelRow, row.model_id)
        if model is None:
            raise AcquisitionError(404, "model_missing", "the owning model no longer exists")
        model.precision = row.precision
        model.total_bytes = row.byte_size
        model.memory_estimate_bytes = row.memory_estimate_bytes
        model.visibility = Visibility.PUBLISHED
    elif request.is_default is False:
        row.is_default = False
    if request.published is not None:
        if request.published is False and row.is_default:
            raise AcquisitionError(
                409,
                "default_must_be_published",
                "select another default before unpublishing this variant",
            )
        row.published = request.published
    if not row.published:
        any_published = (
            await session.execute(
                select(ModelVariantRow.id).where(
                    ModelVariantRow.model_id == row.model_id,
                    ModelVariantRow.id != row.id,
                    ModelVariantRow.published.is_(True),
                )
            )
        ).first()
        if any_published is None:
            model = await session.get(ModelRow, row.model_id)
            if model is not None:
                model.visibility = Visibility.ADMIN_ONLY
    row.updated_at = datetime.now(UTC)
    await write_audit(
        session,
        actor=actor,
        action="variant.publish" if row.published else "variant.unpublish",
        target_type="model_variant",
        target_id=str(row.id),
        detail={"published": row.published, "is_default": row.is_default},
    )
    return variant_projection(row)
