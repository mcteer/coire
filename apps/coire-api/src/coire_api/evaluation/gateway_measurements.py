"""Private exact-resident serving probes through ordinary gateway leases/accounting."""

import asyncio
import uuid
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import String, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    EngineProcessRow,
    EvaluationAttemptRow,
    EvaluationMeasurementRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelRow,
    ModelVariantRow,
    NodeRow,
    TrainingAdapterRow,
    VariantCopyRow,
    session_scope,
)
from coire_api.evaluation.authorization import authorize_live_evaluation_action
from coire_api.gateway.execution import canonical_text_payload, track_stream
from coire_api.gateway.proxy import StreamTiming, stream
from coire_api.gateway.resolution import ResolvedModel
from coire_api.gateway.usage import UsageTracker
from coire_api.placement.service import lock_nodes_for_admission
from coire_core.errors import EvaluationConflict
from coire_core.evaluation_suites.measurement import PROMPTS, prompt_digest
from coire_core.models.acquisition import VariantState
from coire_core.models.adapters import InferenceTarget
from coire_core.models.engine import EngineState
from coire_core.models.evaluation import (
    EvaluationMeasurementRequest,
    EvaluationProbeCompletion,
    EvaluationProbePrepared,
    EvaluationProbePrompt,
    EvaluationResident,
)
from coire_core.models.gateway import ChatMessage, GatewayProtocol, UsageOutcome
from coire_core.models.instance import InstanceState
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.placement import ReservationHolder
from coire_core.models.registry import EngineBackend, ModelState, VisualCapability
from coire_core.settings import Settings
from coire_scheduler.evaluation_guard import COUNTED, engine_binding


@dataclass(frozen=True)
class ProbeScope:
    principal: Principal
    measurement_id: uuid.UUID
    resident: EvaluationResident
    prompt: EvaluationProbePrompt
    engine_url: str


_scope: ContextVar[ProbeScope | None] = ContextVar("evaluation_probe", default=None)


async def authorize(
    session: AsyncSession,
    principal: Principal,
    identity: uuid.UUID,
    resident: EvaluationResident,
    prompt: EvaluationProbePrompt,
) -> ResolvedModel:
    owner = await authorize_live_evaluation_action(session, principal)
    row = await session.get(EvaluationMeasurementRow, identity, populate_existing=True)
    if (
        row is None
        or row.state != "running"
        or row.owner_user_id != owner
        or row.deadline_at <= datetime.now(UTC)
        or Principal.model_validate(row.authorization_snapshot) != principal
    ):
        raise EvaluationConflict("Evaluation measurement authority ended")
    request = EvaluationMeasurementRequest.model_validate(row.request)
    prepared = EvaluationProbePrepared.model_validate(row.execution.get("probes"))
    if (
        resident not in request.resident_targets
        or prompt not in prepared.prompts
        or prepared.measurement_id != row.id
        or prepared.prompt_set_sha256 != prompt_digest()
        or [item.text for item in prepared.prompts] != list(PROMPTS)
    ):
        raise EvaluationConflict("Serving probe differs from its installed frozen workload")
    node = await session.scalar(select(NodeRow).where(NodeRow.name == request.node))
    if node is None:
        raise EvaluationConflict("Measurement node disappeared")
    await lock_nodes_for_admission(session, [node.id])
    return await resolve_probe_residents(session, row, request, resident, node)


async def resolve_probe_residents(
    session: AsyncSession,
    measurement: EvaluationMeasurementRow,
    request: EvaluationMeasurementRequest,
    resident: EvaluationResident,
    node: NodeRow,
) -> ResolvedModel:
    """Read all counted occupancy and registry/process bindings in one fresh query.

    This internal human-admin probe has no selector fallback. The ordinary gateway
    still owns request leases, accounting and upstream I/O. Never cache this snapshot
    across requests or stream lease checks.
    """
    if node.role is not NodeRole.STUDIO or node.reachability is not Reachability.HEALTHY:
        raise EvaluationConflict("Measurement node is unavailable")
    digests = (
        select(
            VariantCopyRow.variant_id.label("variant_id"),
            func.count(func.distinct(VariantCopyRow.manifest_sha256)).label("count"),
            func.max(VariantCopyRow.manifest_sha256).label("sha256"),
        )
        .where(VariantCopyRow.verified.is_(True))
        .group_by(VariantCopyRow.variant_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                MemoryReservationRow,
                ModelInstanceRow,
                InstanceMemberRow,
                EngineProcessRow,
                ModelRow,
                ModelVariantRow,
                VariantCopyRow,
                TrainingAdapterRow,
                EvaluationAttemptRow.id,
                digests.c.count,
                digests.c.sha256,
            )
            .outerjoin(
                ModelInstanceRow,
                cast(ModelInstanceRow.id, String) == MemoryReservationRow.holder_id,
            )
            .outerjoin(
                InstanceMemberRow,
                and_(
                    InstanceMemberRow.instance_id == ModelInstanceRow.id,
                    InstanceMemberRow.reservation_id == MemoryReservationRow.id,
                ),
            )
            .outerjoin(EngineProcessRow, EngineProcessRow.id == InstanceMemberRow.engine_id)
            .outerjoin(ModelRow, ModelRow.id == ModelInstanceRow.model_id)
            .outerjoin(ModelVariantRow, ModelVariantRow.id == ModelInstanceRow.variant_id)
            .outerjoin(
                VariantCopyRow,
                and_(
                    VariantCopyRow.variant_id == ModelInstanceRow.variant_id,
                    VariantCopyRow.node_id == node.id,
                ),
            )
            .outerjoin(TrainingAdapterRow, TrainingAdapterRow.id == ModelInstanceRow.adapter_id)
            .outerjoin(
                EvaluationAttemptRow,
                and_(
                    EvaluationAttemptRow.instance_id == ModelInstanceRow.id,
                    EvaluationAttemptRow.run_id == measurement.execution.get("run_id"),
                    EvaluationAttemptRow.node_id == node.id,
                    EvaluationAttemptRow.owns_instance.is_(True),
                    EvaluationAttemptRow.state != "released",
                ),
            )
            .outerjoin(digests, digests.c.variant_id == ModelInstanceRow.variant_id)
            .where(
                MemoryReservationRow.node_id == node.id,
                MemoryReservationRow.holder_type.in_(
                    [ReservationHolder.MODEL, ReservationHolder.TRAINING]
                ),
                MemoryReservationRow.state.in_(COUNTED),
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    current: dict[uuid.UUID, InferenceTarget] = {}
    bindings: dict[str, str] = {}
    resolved: ResolvedModel | None = None
    for hold, instance, member, engine, model, variant, copy, adapter, owned, count, digest in rows:
        if hold.holder_type is ReservationHolder.TRAINING:
            raise EvaluationConflict("Training reservation prevents evaluation measurement")
        if owned is not None:
            continue
        if (
            instance is None
            or member is None
            or engine is None
            or model is None
            or variant is None
            or copy is None
            or instance.state is not InstanceState.READY
            or not instance.policy.startswith("single:")
            or member.node_id != node.id
            or engine.node_id != node.id
            or engine.instance_id != instance.id
            or engine.model_id != model.id
            or engine.variant_id != variant.id
            or engine.adapter_id != instance.adapter_id
            or engine.state is not EngineState.READY
            or engine.backend != model.backend
            or model.state is not ModelState.READY
            or (model.source or "studio") != "studio"
            or model.backend not in {"mlx_lm", "mlx_vlm"}
            or variant.model_id != model.id
            or not variant.validated
            or variant.state is not VariantState.READY
            or not copy.verified
            or count != 1
            or not digest
            or copy.manifest_sha256 != digest
            or instance.id in current
        ):
            raise EvaluationConflict("Measured resident registry or process binding changed")
        if instance.adapter_id is not None and (
            adapter is None
            or adapter.state != "ready"
            or adapter.purpose == "evaluation"
            or adapter.model_id != model.id
            or adapter.base_variant_id != variant.id
            or adapter.base_manifest_sha256 != digest
            or not adapter.manifest_sha256
            or adapter.selector != f"{model.id}@{adapter.slug}"
            or model.backend != "mlx_lm"
        ):
            raise EvaluationConflict("Measured adapter binding changed")
        target = InferenceTarget(
            model_id=model.id,
            variant_id=variant.id,
            adapter_id=instance.adapter_id,
            base_manifest_sha256=digest,
            adapter_manifest_sha256=adapter.manifest_sha256 if adapter else None,
        )
        current[instance.id] = target
        bindings[str(instance.id)] = engine_binding(engine)
        if instance.id == resident.instance_id:
            resolved = ResolvedModel(
                model.id,
                model.slug,
                model.context_window,
                copy.path,
                engine.id,
                node.name,
                f"http://{node.name}.lab:9400/node/engines/{engine.id}/proxy",
                EngineBackend(model.backend),
                VisualCapability.model_validate(model.visual_capability)
                if model.visual_capability is not None
                else None,
                target=target,
                instance_id=instance.id,
            )
    expected = {item.instance_id: item.target for item in request.resident_targets}
    if current != expected or bindings != measurement.execution.get("resident_engines"):
        raise EvaluationConflict("Measured resident set or engine identity changed")
    if resolved is None or resolved.target != resident.target or not resolved.model_path:
        raise EvaluationConflict("Exact serving probe target is unavailable")
    return resolved


async def authorize_probe_lease(session: AsyncSession, engine_url: str) -> None:
    scope = _scope.get()
    if scope is not None:
        resolved = await authorize(
            session, scope.principal, scope.measurement_id, scope.resident, scope.prompt
        )
        if resolved.engine_url != engine_url or scope.engine_url != engine_url:
            raise EvaluationConflict("Serving probe endpoint changed")


async def generate(
    settings: Settings,
    principal: Principal,
    identity: uuid.UUID,
    resident: EvaluationResident,
    prompt: EvaluationProbePrompt,
    max_output_tokens: int,
) -> EvaluationProbeCompletion:
    began = datetime.now(UTC)
    timing = StreamTiming()
    async with session_scope() as session:
        resolved = await authorize(session, principal, identity, resident, prompt)
        row = await session.get(EvaluationMeasurementRow, identity)
        assert row is not None
        request = EvaluationMeasurementRequest.model_validate(row.request)
        deadline = row.deadline_at
        if max_output_tokens != request.max_output_tokens:
            raise EvaluationConflict("Serving probe output bound changed")
        if principal.api_key_id is not None:
            from coire_api.identity.limits import enforce_limits

            await enforce_limits(session, principal.api_key_id)
    assert resolved.engine_url is not None and resolved.model_path is not None
    usage = UsageTracker(
        principal, str(resident.target.model_id), GatewayProtocol.OPENAI, started_at=began
    )
    usage.bind_resolution(resolved)
    token = _scope.set(ProbeScope(principal, identity, resident, prompt, resolved.engine_url))
    try:
        source = stream(
            resolved.engine_url,
            canonical_text_payload(
                [ChatMessage(role="user", content=prompt.text)],
                resolved.model_path,
                output_tokens=max_output_tokens,
            ),
            settings,
            timing,
        )
        async with asyncio.timeout(max(0, (deadline - datetime.now(UTC)).total_seconds())):
            async with aclosing(track_stream(source, usage, timing=timing)) as tracked:
                async for _ in tracked:
                    async with session_scope() as session:
                        await authorize_probe_lease(session, resolved.engine_url)
        if (
            usage.outcome is not UsageOutcome.SUCCEEDED
            or not usage.reported_token_usage
            or usage.first_token_duration_ms is None
            or timing.upstream_started_at is None
            or timing.first_chunk_at is None
            or usage.prompt_tokens != prompt.tokens_by_instance[resident.instance_id]
            or not 1 <= usage.completion_tokens <= max_output_tokens
        ):
            raise EvaluationConflict("Serving probe lacks complete exact token and timing evidence")
        return EvaluationProbeCompletion(
            instance_id=resident.instance_id,
            first_token_seconds=usage.first_token_duration_ms / 1000,
            gateway_seconds=max(0, timing.upstream_started_at - timing.request_started_at),
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
        )
    except BaseException:
        await usage.finish(UsageOutcome.FAILED, failure_code="evaluation_probe_failed")
        raise
    finally:
        _scope.reset(token)
