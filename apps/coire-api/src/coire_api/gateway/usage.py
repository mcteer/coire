"""Exactly-once, cancellation-resistant gateway accounting."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from coire_api.auth import Principal
from coire_api.db import UsageRecordRow, session_scope
from coire_core.models.gateway import GatewayProtocol, UsageOutcome


async def persist_usage(
    *,
    request_id: uuid.UUID,
    principal: Principal,
    model_id: uuid.UUID,
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
