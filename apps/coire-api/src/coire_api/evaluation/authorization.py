"""Human-owned privileged evaluation admission and exact judge identity checks."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request
from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import CurrentAdmin, CurrentPrincipal, Principal, PrincipalKind
from coire_api.db import session_scope
from coire_api.nodes_client import NodeClient
from coire_api.training.authorization import (
    authorize_live_training_action,
    preflight_training_action,
)
from coire_core.errors import EvaluationForbidden, EvaluationSelfJudge, TrainingForbidden
from coire_core.models.adapters import InferenceTarget
from coire_core.models.audit import AuditOutcome
from coire_core.models.evaluation import EvaluationSubject, EvaluationTarget
from coire_core.settings import get_settings

tracer = trace.get_tracer("coire.api.evaluation")
logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from coire_api.db import TrainingAdapterRow


def preflight_evaluation_action(
    principal: Principal, *, method: str, origin: str | None, browser_origin: str
) -> uuid.UUID:
    try:
        return preflight_training_action(
            principal, method=method, origin=origin, browser_origin=browser_origin
        )
    except TrainingForbidden as error:
        raise EvaluationForbidden() from error


async def authorize_live_evaluation_action(
    session: AsyncSession, principal: Principal
) -> uuid.UUID:
    with tracer.start_as_current_span(
        "coire.api.evaluation.authorize", record_exception=False, set_status_on_exception=False
    ):
        try:
            return await authorize_live_training_action(session, principal)
        except TrainingForbidden as error:
            raise EvaluationForbidden() from error


def reject_self_judge(subjects: Sequence[InferenceTarget], judge: InferenceTarget | None) -> None:
    if judge is not None and any(
        subject.model_id == judge.model_id
        or subject.base_manifest_sha256 == judge.base_manifest_sha256
        for subject in subjects
    ):
        raise EvaluationSelfJudge(
            "Candidate and judge must have distinct model and base artifact identities"
        )


async def require_evaluation_principal(request: Request, principal: CurrentPrincipal) -> Principal:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    try:
        preflight_evaluation_action(
            principal,
            method=request.method,
            origin=request.headers.get("origin"),
            browser_origin=settings.chat_browser_origin,
        )
        async with session_scope() as session:
            await authorize_live_evaluation_action(session, principal)
    except EvaluationForbidden:
        async with session_scope() as session:
            await write_principal_audit(
                session,
                principal=principal,
                action="evaluation.refused",
                target_type="route",
                target_id=f"{request.method} {request.url.path}",
                outcome=AuditOutcome.REFUSED,
                context={"reason": "authorization"},
            )
            await session.commit()
        logger.info("evaluation admission refused", extra={"safe_reason": "authorization"})
        raise
    return principal


CurrentEvaluationAdmin = Annotated[Principal, Depends(require_evaluation_principal)]


async def authorize_evaluation_read(
    session: AsyncSession, principal: Principal, *, legacy_enabled: bool
) -> None:
    if principal.user_id is not None:
        await authorize_live_evaluation_action(session, principal)
    elif not (legacy_enabled and principal.kind is PrincipalKind.ADMIN and principal.is_admin):
        raise EvaluationForbidden()


async def require_evaluation_reader(request: Request, principal: CurrentAdmin) -> Principal:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    try:
        async with session_scope() as session:
            await authorize_evaluation_read(
                session, principal, legacy_enabled=settings.identity_legacy_admin_enabled
            )
    except EvaluationForbidden:
        async with session_scope() as session:
            await write_principal_audit(
                session,
                principal=principal,
                action="evaluation.refused",
                target_type="route",
                target_id=f"{request.method} {request.url.path}",
                outcome=AuditOutcome.REFUSED,
                context={"reason": "authorization"},
            )
            await session.commit()
        logger.info("evaluation read refused", extra={"safe_reason": "authorization"})
        raise
    return principal


CurrentEvaluationReader = Annotated[Principal, Depends(require_evaluation_reader)]


async def resolve_evaluation_target(
    session: AsyncSession,
    principal: Principal,
    subject: EvaluationSubject,
    client: NodeClient,
    *,
    checkpoint_measurement: bool = False,
) -> EvaluationTarget:
    from sqlalchemy import select

    from coire_api.db import NodeRow, TrainingAdapterRow, VariantCopyRow
    from coire_api.gateway.targets import ModelNotFoundError, resolve_target
    from coire_api.nodes_client import NodeError
    from coire_core.errors import EvaluationConflict, EvaluationNotFound, EvaluationUnavailable
    from coire_core.models.evaluation import EvaluationIdentityRequest, EvaluationTarget
    from coire_core.models.node import NodeRole, Reachability
    from coire_core.models.registry import CapabilityProfile

    await authorize_live_evaluation_action(session, principal)
    selector: uuid.UUID | str = subject.model_id
    if subject.adapter_id is not None:
        adapter = await session.get(TrainingAdapterRow, subject.adapter_id, populate_existing=True)
        if (
            adapter is None
            or adapter.model_id != subject.model_id
            or adapter.base_variant_id != subject.variant_id
        ):
            raise EvaluationNotFound("Evaluation target is unavailable")
        if adapter.purpose == "evaluation":
            if not checkpoint_measurement:
                raise EvaluationNotFound("Evaluation target is unavailable")
            return await resolve_checkpoint_measurement_target(session, principal, adapter, client)
        selector = adapter.selector
    try:
        resolved = await resolve_target(session, selector, principal, subject.variant_id)
    except ModelNotFoundError:
        raise EvaluationNotFound("Evaluation target is unavailable") from None
    if resolved.identity is None or resolved.variant is None:
        raise EvaluationConflict("Evaluation requires an exact acquired local target")
    from coire_core.models.registry import EngineBackend

    backend = EngineBackend(resolved.model.backend)
    profile = CapabilityProfile.model_validate(resolved.model.capability_profile or {})
    request = EvaluationIdentityRequest(
        engine_backend=backend,
        target=resolved.identity,
        variant_slug=resolved.variant.slug,
        template_override=resolved.model.chat_template,
        capability_profile=profile,
    )
    nodes = (
        await session.scalars(
            select(NodeRow)
            .join(VariantCopyRow, VariantCopyRow.node_id == NodeRow.id)
            .where(
                VariantCopyRow.variant_id == subject.variant_id,
                VariantCopyRow.verified.is_(True),
                VariantCopyRow.manifest_sha256 == resolved.identity.base_manifest_sha256,
                NodeRow.role == NodeRole.STUDIO,
                NodeRow.reachability == Reachability.HEALTHY,
            )
            .order_by(NodeRow.name)
        )
    ).all()
    for node in nodes:
        try:
            runtime = await client.evaluation_identity(node.name, request)
        except NodeError:
            continue
        return EvaluationTarget(
            engine_backend=backend,
            target=resolved.identity,
            public_selector=selector,
            variant_slug=resolved.variant.slug,
            template_override=resolved.model.chat_template,
            capability_profile=profile,
            context_window=resolved.model.context_window or profile.context_window or 4096,
            runtime=runtime,
            display_name=resolved.model.display_name,
        )
    raise EvaluationUnavailable("No Studio can attest the required evaluation runtime")


async def resolve_checkpoint_measurement_target(
    session: AsyncSession, principal: Principal, adapter: TrainingAdapterRow, client: NodeClient
) -> EvaluationTarget:
    from coire_api.db import TrainingJobRow
    from coire_api.evaluation.training import require_checkpoint_pause
    from coire_core.errors import EvaluationConflict
    from coire_core.models.training import ResolvedTrainingSpecV2, parse_resolved_training_spec

    job = await session.get(
        TrainingJobRow, adapter.source_job_id, with_for_update=True, populate_existing=True
    )
    if (
        job is None
        or adapter.evaluation_trigger_id is None
        or adapter.state != "ready"
        or adapter.manifest_sha256 is None
    ):
        raise EvaluationConflict("Checkpoint measurement target is unavailable")
    trigger = await require_checkpoint_pause(session, job, adapter.evaluation_trigger_id)
    if adapter.source_checkpoint_id != trigger.checkpoint_id or trigger.phase not in {
        "evaluating",
        "preparing_adapter",
    }:
        raise EvaluationConflict("Checkpoint measurement ownership is unavailable")
    resolved = parse_resolved_training_spec(job.resolved_spec)
    if not isinstance(resolved, ResolvedTrainingSpecV2):
        raise EvaluationConflict("Checkpoint measurement requires frozen evaluation identity")
    base = await resolve_evaluation_target(
        session,
        principal,
        EvaluationSubject(
            model_id=adapter.model_id,
            variant_id=adapter.base_variant_id,
        ),
        client,
    )
    if base != resolved.evaluation_base:
        raise EvaluationConflict("Checkpoint measurement runtime differs from frozen training")
    return EvaluationTarget.model_validate(
        {
            **base.model_dump(mode="json"),
            "public_selector": adapter.selector,
            "display_name": f"{base.display_name}@{adapter.slug}",
            "target": {
                **base.target.model_dump(mode="json"),
                "adapter_id": adapter.id,
                "adapter_manifest_sha256": adapter.manifest_sha256,
            },
        }
    )
