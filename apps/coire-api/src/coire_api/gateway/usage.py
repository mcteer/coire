"""Exactly-once, cancellation-resistant gateway accounting."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from coire_api.auth import Principal
from coire_api.db import UsageRecordRow, session_scope
from coire_api.gateway.telemetry import (
    failure_counter,
    inflight_counter,
    request_counter,
    request_duration_ms,
)
from coire_core.models.gateway import GatewayProtocol, UsageOutcome


@dataclass(slots=True)
class UsageTracker:
    principal: Principal
    requested_model_id: str
    protocol: GatewayProtocol
    request_id: uuid.UUID = field(default_factory=uuid.uuid4)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    model_id: uuid.UUID | None = None
    engine_id: uuid.UUID | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    _finished: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        inflight_counter.add(1, {"protocol": self.protocol.value})

    async def finish(self, outcome: UsageOutcome, *, failure_code: str | None = None) -> None:
        async with self._lock:
            if self._finished:
                return
            self._finished = True
        attributes = {
            "protocol": self.protocol.value,
            "outcome": outcome.value,
            "failure_code": failure_code or "none",
        }
        duration_ms = max((datetime.now(UTC) - self.started_at).total_seconds() * 1000, 0)
        request_counter.add(1, attributes)
        request_duration_ms.record(duration_ms, attributes)
        inflight_counter.add(-1, {"protocol": self.protocol.value})
        if outcome is UsageOutcome.FAILED:
            failure_counter.add(1, attributes)
        await persist_usage(
            request_id=self.request_id,
            principal=self.principal,
            requested_model_id=self.requested_model_id,
            model_id=self.model_id,
            engine_id=self.engine_id,
            protocol=self.protocol,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            started_at=self.started_at,
            outcome=outcome,
            failure_code=failure_code,
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
) -> None:
    """Insert once even when the request task is being cancelled."""

    async def _write() -> None:
        finished_at = datetime.now(UTC)
        async with session_scope() as session:
            exists = await session.scalar(
                select(UsageRecordRow.id).where(UsageRecordRow.request_id == request_id)
            )
            if exists is not None:
                return
            session.add(
                UsageRecordRow(
                    request_id=request_id,
                    principal_kind=principal.kind.value,
                    principal_subject=principal.subject,
                    requested_model_id=requested_model_id,
                    model_id=model_id,
                    engine_id=engine_id,
                    protocol=protocol,
                    prompt_tokens=max(prompt_tokens, 0),
                    completion_tokens=max(completion_tokens, 0),
                    duration_ms=max((finished_at - started_at).total_seconds() * 1000, 0),
                    outcome=outcome,
                    failure_code=failure_code,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            )

    task = asyncio.create_task(_write())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
