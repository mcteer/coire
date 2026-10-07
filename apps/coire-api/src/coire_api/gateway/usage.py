"""Exactly-once, cancellation-resistant gateway accounting."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert

from coire_api.auth import Principal
from coire_api.db import UsageRecordRow, session_scope
from coire_api.gateway.resolution import ResolvedModel
from coire_api.gateway.telemetry import (
    failure_counter,
    inflight_counter,
    request_counter,
    request_duration_ms,
    token_counter,
    vision_requests_total,
)
from coire_api.identity.limits import settle_usage
from coire_core.models.gateway import GatewayProtocol, UsageOutcome
from coire_core.models.registry import EngineBackend, ModelSource


@dataclass(slots=True)
class UsageTracker:
    principal: Principal
    requested_model_id: str
    protocol: GatewayProtocol
    request_id: uuid.UUID = field(default_factory=uuid.uuid4)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    model_id: uuid.UUID | None = None
    engine_id: uuid.UUID | None = None
    instance_id: uuid.UUID | None = None
    first_token_at: datetime | None = None
    first_token_duration_ms: float | None = None
    reported_token_usage: bool = False
    outcome: UsageOutcome | None = None
    variant_id: uuid.UUID | None = None
    adapter_id: uuid.UUID | None = None
    node: str | None = None
    provider_source: ModelSource = ModelSource.STUDIO
    backend: EngineBackend | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reserved_tokens: int = 0
    _finished: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        inflight_counter.add(1, {"protocol": self.protocol.value})

    def bind_resolution(self, resolved: ResolvedModel) -> None:
        """Attach the registry-selected model and engine to once-only accounting."""
        self.model_id = resolved.model_id
        self.engine_id = resolved.engine_id
        self.instance_id = resolved.instance_id
        self.variant_id = resolved.target.variant_id if resolved.target else None
        self.adapter_id = resolved.target.adapter_id if resolved.target else None
        self.node = resolved.node
        self.provider_source = resolved.source
        self.backend = resolved.backend

    async def finish(self, outcome: UsageOutcome, *, failure_code: str | None = None) -> None:
        async with self._lock:
            if self._finished:
                return
            self._finished = True
            self.outcome = outcome
        attributes = {
            "protocol": self.protocol.value,
            "source": self.provider_source.value,
            "outcome": outcome.value,
            "failure_code": failure_code or "none",
        }
        duration_ms = max((datetime.now(UTC) - self.started_at).total_seconds() * 1000, 0)
        request_counter.add(1, attributes)
        request_duration_ms.record(duration_ms, attributes)
        inflight_counter.add(-1, {"protocol": self.protocol.value})
        if self.backend is EngineBackend.MLX_VLM:
            vision_requests_total.add(1, {"outcome": outcome.value})
        if outcome is UsageOutcome.FAILED:
            failure_counter.add(1, attributes)
        if outcome is UsageOutcome.SUCCEEDED:
            token_counter.add(
                max(self.prompt_tokens, 0) + max(self.completion_tokens, 0),
                {"source": self.provider_source.value, "protocol": self.protocol.value},
            )
        await persist_usage(
            request_id=self.request_id,
            principal=self.principal,
            requested_model_id=self.requested_model_id,
            model_id=self.model_id,
            engine_id=self.engine_id,
            instance_id=self.instance_id,
            first_token_at=self.first_token_at,
            first_token_duration_ms=self.first_token_duration_ms,
            variant_id=self.variant_id,
            adapter_id=self.adapter_id,
            protocol=self.protocol,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            started_at=self.started_at,
            outcome=outcome,
            failure_code=failure_code,
            reserved_tokens=self.reserved_tokens,
            provider_source=self.provider_source,
        )


async def persist_usage(
    *,
    request_id: uuid.UUID,
    principal: Principal,
    requested_model_id: str,
    model_id: uuid.UUID | None,
    engine_id: uuid.UUID | None,
    protocol: GatewayProtocol,
    prompt_tokens: int,
    completion_tokens: int,
    started_at: datetime,
    outcome: UsageOutcome,
    failure_code: str | None = None,
    reserved_tokens: int = 0,
    provider_source: ModelSource = ModelSource.STUDIO,
    variant_id: uuid.UUID | None = None,
    adapter_id: uuid.UUID | None = None,
    instance_id: uuid.UUID | None = None,
    first_token_at: datetime | None = None,
    first_token_duration_ms: float | None = None,
) -> None:
    """Insert once even when the request task is being cancelled."""

    async def _write() -> None:
        finished_at = datetime.now(UTC)
        async with session_scope() as session:
            inserted = await session.scalar(
                insert(UsageRecordRow)
                .values(
                    request_id=request_id,
                    principal_kind=principal.kind.value,
                    principal_subject=principal.subject,
                    requested_model_id=requested_model_id,
                    model_id=model_id,
                    engine_id=engine_id,
                    instance_id=instance_id,
                    first_token_at=first_token_at,
                    first_token_duration_ms=first_token_duration_ms,
                    variant_id=variant_id,
                    adapter_id=adapter_id,
                    protocol=protocol,
                    prompt_tokens=max(prompt_tokens, 0),
                    completion_tokens=max(completion_tokens, 0),
                    duration_ms=max((finished_at - started_at).total_seconds() * 1000, 0),
                    outcome=outcome,
                    failure_code=failure_code,
                    started_at=started_at,
                    finished_at=finished_at,
                )
                .on_conflict_do_nothing(index_elements=[UsageRecordRow.request_id])
                .returning(UsageRecordRow.id)
            )
            if inserted is None:
                return
            if principal.api_key_id is not None:
                await settle_usage(
                    session,
                    principal.api_key_id,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            if principal.run_id is not None and reserved_tokens > 0:
                from coire_api.run_tokens import settle_run_token_usage

                await settle_run_token_usage(
                    session,
                    principal.run_id,
                    max(prompt_tokens, 0) + max(completion_tokens, 0),
                    reserved_tokens=reserved_tokens,
                )
            if outcome is UsageOutcome.SUCCEEDED and provider_source is not ModelSource.STUDIO:
                from coire_api.gateway.provider_budget import settle_provider_budget

                await settle_provider_budget(
                    session,
                    request_id,
                    actual_tokens=max(prompt_tokens, 0) + max(completion_tokens, 0),
                )

    task = asyncio.create_task(_write())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
