"""Audited human-admin admission of measured same-node image/chat profiles."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.auth import Principal, audit_actor
from coire_api.db import (
    ImageCoexistenceProfileRow,
    ModelRow,
    ModelVariantRow,
    NodeRow,
)
from coire_core.errors import ImageNotFound, ImageValidationError
from coire_core.models.acquisition import VariantState
from coire_core.models.audit import AuditOutcome
from coire_core.models.images import (
    ImageCapabilityProfile,
    ImageCoexistenceProfile,
    ImageCoexistenceReportRequest,
    ImageMode,
)
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.registry import (
    EngineBackend,
    ModelKind,
    ModelSource,
    ModelState,
    Visibility,
)
from coire_scheduler.image_admission import (
    coexistence_report_hash,
    node_hardware_fingerprint,
    node_runtime_fingerprint,
)

_MAX_REPORT_AGE = timedelta(days=1)
_MAX_PROFILE_VALIDITY = timedelta(days=7)


def project_coexistence_profile(row: ImageCoexistenceProfileRow) -> ImageCoexistenceProfile:
    return ImageCoexistenceProfile(
        id=row.id,
        profile_hash=row.profile_hash,
        status="invalidated" if row.invalidated_at is not None else "approved",
        report=ImageCoexistenceReportRequest.model_validate(row.benchmark_result),
        invalidated_at=row.invalidated_at,
    )


async def admit_coexistence_report(
    session: AsyncSession,
    principal: Principal,
    report: ImageCoexistenceReportRequest,
) -> ImageCoexistenceProfile:
    """Store only current, passing evidence for a ready node/model/variant set."""
    now = datetime.now(UTC)
    if (
        report.measured_at > now
        or report.measured_at < now - _MAX_REPORT_AGE
        or report.valid_until <= now
        or report.valid_until > report.measured_at + _MAX_PROFILE_VALIDITY
    ):
        raise ImageValidationError("coexistence report is stale or has invalid validity")
    node = await session.get(NodeRow, report.node_id, populate_existing=True, with_for_update=True)
    if (
        node is None
        or node.role is not NodeRole.STUDIO
        or node.reachability is not Reachability.HEALTHY
    ):
        raise ImageValidationError("coexistence node unavailable")
    if report.hardware_fingerprint != node_hardware_fingerprint(
        node
    ) or report.runtime_fingerprint != node_runtime_fingerprint(node):
        raise ImageValidationError("coexistence node fingerprint changed")
    model = await session.get(
        ModelRow, report.image_model_id, populate_existing=True, with_for_update=True
    )
    if (
        model is None
        or model.kind is not ModelKind.IMAGE_MODEL
        or model.backend != EngineBackend.MFLUX
        or model.source is not ModelSource.STUDIO
        or model.state is not ModelState.READY
        or model.visibility is not Visibility.PUBLISHED
        or model.image_capability_profile is None
    ):
        raise ImageValidationError("coexistence image model unavailable")
    try:
        capability = ImageCapabilityProfile.model_validate(model.image_capability_profile)
    except ValidationError as exc:
        raise ImageValidationError("coexistence image capability unavailable") from exc
    bounds = report.measured_bounds
    if (
        ImageMode.TXT2IMG not in capability.modes
        or bounds.max_width > capability.max_width
        or bounds.max_height > capability.max_height
        or bounds.max_width * bounds.max_height > capability.max_pixels
        or bounds.max_steps > capability.max_steps
        or bounds.max_outputs > capability.max_outputs
    ):
        raise ImageValidationError("coexistence bounds exceed validated capability")
    for variant_id in report.chat_variant_ids:
        variant = await session.get(
            ModelVariantRow, variant_id, populate_existing=True, with_for_update=True
        )
        if (
            variant is None
            or variant.backend not in {EngineBackend.MLX_LM.value, EngineBackend.MLX_VLM.value}
            or variant.state is not VariantState.READY
            or not variant.validated
            or not variant.published
        ):
            raise ImageValidationError("coexistence chat variant unavailable")
        chat_model = await session.get(ModelRow, variant.model_id)
        if (
            chat_model is None
            or chat_model.kind is not ModelKind.LANGUAGE_MODEL
            or chat_model.state is not ModelState.READY
        ):
            raise ImageValidationError("coexistence chat model unavailable")
    canonical = report.model_copy(
        update={"chat_variant_ids": tuple(sorted(report.chat_variant_ids))}
    )
    evidence = canonical.model_dump(mode="json")
    digest = coexistence_report_hash(canonical)
    row = ImageCoexistenceProfileRow(
        id=uuid.uuid4(),
        profile_hash=digest,
        node_id=report.node_id,
        hardware_fingerprint=report.hardware_fingerprint,
        runtime_fingerprint=report.runtime_fingerprint,
        chat_variant_ids=[str(item) for item in canonical.chat_variant_ids],
        image_model_id=report.image_model_id,
        image_mode=report.image_mode,
        measured_bounds=bounds.model_dump(mode="json"),
        benchmark_result=evidence,
        first_token_p95_ms=report.first_token_p95_ms,
        status="approved",
        valid_until=report.valid_until,
        created_at=now,
    )
    session.add(row)
    actor, actor_type, actor_user_id = audit_actor(principal)
    await write_audit(
        session,
        actor=actor,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action="image.coexistence.approved",
        target_type="image_coexistence_profile",
        target_id=str(row.id),
        outcome=AuditOutcome.OK,
        context={
            "node_id": str(report.node_id),
            "image_model_id": str(report.image_model_id),
            "profile_hash": digest,
        },
    )
    await session.flush()
    return project_coexistence_profile(row)


async def invalidate_coexistence_profile(
    session: AsyncSession, principal: Principal, profile_id: uuid.UUID
) -> ImageCoexistenceProfile:
    """Stop future mixed admission as soon as the audited row commits."""
    row = await session.get(
        ImageCoexistenceProfileRow, profile_id, populate_existing=True, with_for_update=True
    )
    if row is None:
        raise ImageNotFound()
    if row.invalidated_at is None:
        row.invalidated_at = datetime.now(UTC)
        row.status = "invalidated"
        actor, actor_type, actor_user_id = audit_actor(principal)
        await write_audit(
            session,
            actor=actor,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            action="image.coexistence.invalidated",
            target_type="image_coexistence_profile",
            target_id=str(profile_id),
            outcome=AuditOutcome.OK,
        )
    return project_coexistence_profile(row)
