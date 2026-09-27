"""Atomic database-backed API-key rate and monthly token accounting."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from opentelemetry import metrics
from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ApiKeyRow, RateWindowRow, UsageAccumulatorRow
from coire_api.identity.windows import minute_window, month_window

limit_refusals = metrics.get_meter("coire.api.auth").create_counter(
    "coire_key_limit_refusals_total", unit="1"
)


@dataclass
class RateLimitExceeded(Exception):
    retry_at: datetime


@dataclass
class MonthlyQuotaExceeded(Exception):
    budget_tokens: int
    consumed_tokens: int
    resets_at: datetime


async def admit_rate(session: AsyncSession, key_id: uuid.UUID, limit: int) -> None:
    start, end = minute_window()
    table = cast(Table, RateWindowRow.__table__)
    statement = (
        insert(table)
        .values(api_key_id=key_id, window_start=start, window_end=end, requests=1)
        .on_conflict_do_update(
            index_elements=[table.c.api_key_id, table.c.window_start],
            set_={"requests": table.c.requests + 1},
            where=table.c.requests < limit,
        )
        .returning(table.c.requests)
    )
    if await session.scalar(statement) is None:
        limit_refusals.add(1, {"kind": "rate"})
        raise RateLimitExceeded(end)


async def check_quota(session: AsyncSession, key: ApiKeyRow) -> None:
    start, end = month_window()
    row = await session.get(UsageAccumulatorRow, (key.id, start))
    consumed = (row.prompt_tokens + row.completion_tokens) if row else 0
    if consumed >= key.monthly_budget_tokens:
        limit_refusals.add(1, {"kind": "quota"})
        raise MonthlyQuotaExceeded(key.monthly_budget_tokens, consumed, end)


async def settle_usage(
    session: AsyncSession, key_id: uuid.UUID, *, prompt_tokens: int, completion_tokens: int
) -> None:
    start, end = month_window()
    table = cast(Table, UsageAccumulatorRow.__table__)
    statement = insert(table).values(
        api_key_id=key_id,
        period_start=start,
        period_end=end,
        requests=1,
        prompt_tokens=max(0, prompt_tokens),
        completion_tokens=max(0, completion_tokens),
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[table.c.api_key_id, table.c.period_start],
            set_={
                "requests": table.c.requests + 1,
                "prompt_tokens": table.c.prompt_tokens + statement.excluded.prompt_tokens,
                "completion_tokens": (
                    table.c.completion_tokens + statement.excluded.completion_tokens
                ),
            },
        )
    )


async def enforce_limits(session: AsyncSession, key_id: uuid.UUID) -> ApiKeyRow:
    key = await session.scalar(select(ApiKeyRow).where(ApiKeyRow.id == key_id).with_for_update())
    if key is None or key.revoked_at is not None:
        raise LookupError("active API key not found")
    await admit_rate(session, key.id, key.requests_per_minute)
    await check_quota(session, key)
    return key
