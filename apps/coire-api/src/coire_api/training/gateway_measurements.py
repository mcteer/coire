"""Private authenticated exact-instance generation for measured Studio workloads.

The context is task-local, never accepted from a public request. The proxy rechecks
it inside the same node admission locks used by ordinary request leases.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    MemoryReservationRow,
    NodeRow,
    TrainingAdapterRow,
    TrainingCommandRow,
    TrainingMeasurementRow,
    session_scope,
)
from coire_api.gateway.execution import canonical_text_payload, track_stream
from coire_api.gateway.proxy import StreamTiming, stream
from coire_api.gateway.resolution import ResolvedModel, resolve_model
from coire_api.gateway.usage import UsageTracker
from coire_api.placement.service import lock_nodes_for_admission
from coire_api.training.authorization import authorize_live_training_action
from coire_api.training.measurements import validate_measurement_prompts
from coire_api.training.service import payload_digest
from coire_api.training.telemetry import observed
from coire_core.errors import TrainingConflict
from coire_core.models.gateway import ChatMessage, GatewayProtocol, UsageOutcome
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.training import (
    TrainingMeasurementCompletion,
    TrainingMeasurementPrompt,
    TrainingMeasurementPromptSet,
    TrainingMeasurementRequest,
    TrainingResidentTarget,
)
from coire_core.models.training_node import TrainingMeasurementDispatch
from coire_core.settings import Settings


@dataclass(frozen=True)
class _MeasurementScope:
    principal: Principal
    measurement_id: uuid.UUID
    target: TrainingResidentTarget
    prompt: TrainingMeasurementPrompt
    max_output_tokens: int
    engine_url: str


_scope: ContextVar[_MeasurementScope | None] = ContextVar("gateway_measurement", default=None)


async def _authorize(
    session: AsyncSession,
    principal: Principal,
    measurement_id: uuid.UUID,
    target: TrainingResidentTarget,
    prompt: TrainingMeasurementPrompt,
    max_output_tokens: int,
) -> ResolvedModel:
    from coire_scheduler.training_guard import current_residents
    from coire_scheduler.training_measurements import resident_members

    owner = await authorize_live_training_action(session, principal)
    row = await session.get(TrainingMeasurementRow, measurement_id, populate_existing=True)
    command = await session.scalar(
        select(TrainingCommandRow).where(
            TrainingCommandRow.operation == "training.measurement",
            TrainingCommandRow.subject_id == str(measurement_id),
        )
    )
    if row is None or command is None or row.state != "running" or row.owner_user_id != owner:
        raise TrainingConflict("Measurement gateway authority ended")
    request = TrainingMeasurementRequest.model_validate(row.request)
    prompts = TrainingMeasurementPromptSet.model_validate(command.payload.get("prompts"))
    dispatch = TrainingMeasurementDispatch.model_validate(command.payload.get("dispatch"))
    validate_measurement_prompts(request, prompts)
    if (
        request.mode != "coexistence"
        or command.actor_user_id != owner
        or Principal.model_validate(command.payload["principal"]) != principal
        or command.request_sha256 != payload_digest(request)
        or target not in request.resident_targets
        or prompt not in prompts.prompts
        or max_output_tokens != request.workload.max_output_tokens
        or not dispatch.commands
        or sorted(p.prepare.node for p in dispatch.commands) != sorted(request.nodes)
        or any(
            p.measurement_id != measurement_id
            or p.mode != "coexistence"
            or p.resident_targets != request.resident_targets
            or p.deadline <= datetime.now(UTC)
            for p in dispatch.commands
        )
    ):
        raise TrainingConflict("Generation differs from frozen measured workload")
    nodes = list(await session.scalars(select(NodeRow).where(NodeRow.name.in_(request.nodes))))
    if len(nodes) != len(request.nodes):
        raise TrainingConflict("Measurement node inventory changed")
    await lock_nodes_for_admission(session, [node.id for node in nodes])
    residents = await current_residents(session, [node.id for node in nodes])
    if residents != sorted(request.resident_targets, key=lambda item: str(item.instance_id)):
        raise TrainingConflict("Measurement resident set changed")
    for resident in request.resident_targets:
        members = await resident_members(session, resident)
        expected_engines = {
            instance_id: engine_id
            for probe in dispatch.commands
            for instance_id, engine_id in probe.resident_engine_ids.items()
        }
        if expected_engines.get(resident.instance_id) != members[0].engine_id:
            raise TrainingConflict("Measured resident engine identity changed")
    holds = list(
        await session.scalars(
            select(MemoryReservationRow)
            .where(
                MemoryReservationRow.node_id.in_([node.id for node in nodes]),
                MemoryReservationRow.holder_type == ReservationHolder.TRAINING,
                MemoryReservationRow.state.in_(
                    [
                        MemoryReservationState.PENDING,
                        MemoryReservationState.HELD,
                        MemoryReservationState.RELEASING,
                    ]
                ),
            )
            .with_for_update()
        )
    )
    if {h.id for h in holds} != {p.prepare.reservation_id for p in dispatch.commands} or any(
        h.state is not MemoryReservationState.HELD or h.holder_id != f"measurement:{measurement_id}"
        for h in holds
    ):
        raise TrainingConflict("Dedicated measurement admission hold ended")
    adapter = (
        await session.get(TrainingAdapterRow, target.target.adapter_id)
        if target.target.adapter_id
        else None
    )
    selector = adapter.selector if adapter else str(target.target.model_id)
    resolved = await resolve_model(
        session,
        selector,
        principal,
        variant_id=target.target.variant_id,
        instance_id=target.instance_id,
    )
    if (
        resolved.instance_id != target.instance_id
        or resolved.target != target.target
        or resolved.engine_url is None
        or resolved.model_path is None
    ):
        raise TrainingConflict("Exact measured instance is unavailable")
    return resolved


async def authorize_measurement_lease(session: AsyncSession, engine_url: str) -> None:
    """Proxy-only hook; ordinary admission and guard checks still run afterward."""
    scope = _scope.get()
    if scope is None:
        return
    resolved = await _authorize(
        session,
        scope.principal,
        scope.measurement_id,
        scope.target,
        scope.prompt,
        scope.max_output_tokens,
    )
    if engine_url != scope.engine_url or resolved.engine_url != engine_url:
        raise TrainingConflict("Measurement proxy endpoint changed")


def gateway_measurement_generate(
    settings: Settings,
) -> Callable[
    [Principal, uuid.UUID, TrainingResidentTarget, TrainingMeasurementPrompt, int],
    Awaitable[TrainingMeasurementCompletion],
]:
    """Construct the scheduler's private callback from its configured settings."""

    @observed("coire.api.training.measurement.gateway")
    async def generate(
        principal: Principal,
        measurement_id: uuid.UUID,
        target: TrainingResidentTarget,
        prompt: TrainingMeasurementPrompt,
        max_output_tokens: int,
    ) -> TrainingMeasurementCompletion:
        if not settings.training_enabled:
            raise TrainingConflict("Training measurements are disabled")
        began = datetime.now(UTC)
        timing = StreamTiming()
        async with session_scope() as session:
            resolved = await _authorize(
                session,
                principal,
                measurement_id,
                target,
                prompt,
                max_output_tokens,
            )
            if principal.api_key_id is not None:
                from coire_api.identity.limits import enforce_limits

                await enforce_limits(session, principal.api_key_id)
        assert resolved.engine_url is not None and resolved.model_path is not None
        usage = UsageTracker(
            principal, str(target.target.model_id), GatewayProtocol.OPENAI, started_at=began
        )
        usage.bind_resolution(resolved)
        scope = _MeasurementScope(
            principal,
            measurement_id,
            target,
            prompt,
            max_output_tokens,
            resolved.engine_url,
        )
        token = _scope.set(scope)
        try:
            payload = canonical_text_payload(
                [ChatMessage(role="user", content=prompt.text)],
                resolved.model_path,
                output_tokens=max_output_tokens,
            )
            source = stream(resolved.engine_url, payload, settings, timing)
            async with aclosing(track_stream(source, usage, timing=timing)) as tracked:
                async for _ in tracked:
                    async with session_scope() as session:
                        await authorize_measurement_lease(session, resolved.engine_url)
            if (
                usage.outcome is not UsageOutcome.SUCCEEDED
                or not usage.reported_token_usage
                or usage.first_token_duration_ms is None
                or usage.instance_id is None
                or resolved.target is None
                or usage.prompt_tokens
                != prompt.tokens_by_instance.get(target.instance_id, prompt.input_tokens)
                or not 1 <= usage.completion_tokens <= max_output_tokens
            ):
                raise TrainingConflict("Gateway stream lacks exact completed token evidence")
            return TrainingMeasurementCompletion(
                instance_id=usage.instance_id,
                target=resolved.target,
                first_token_seconds=usage.first_token_duration_ms / 1000,
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
            )
        except BaseException:
            await usage.finish(UsageOutcome.FAILED, failure_code="measurement_gateway_failed")
            raise
        finally:
            _scope.reset(token)

    return generate
