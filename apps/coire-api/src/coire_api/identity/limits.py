"""Atomic database-backed API-key rate and monthly token accounting."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn, cast

from opentelemetry import metrics
from sqlalchemy import Integer, Table, bindparam, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import CTE

from coire_api.db import ApiKeyRow, RateWindowRow, UsageAccumulatorRow
from coire_api.identity.windows import minute_window, month_window

limit_refusals = metrics.get_meter("coire.api.auth").create_counter(
    "coire_key_limit_refusals_total", unit="1"
)


# Fixed bound SQL avoids PostgreSQL upsert recompilation on every admission.
# All authority values come from the caller's current, mutation-locked key row.
_LOCKED_KEY_ADMISSION = text("""
    INSERT INTO api_key_rate_windows (api_key_id, window_start, window_end, requests)
    SELECT :key_id, :minute_start, :minute_end, 1
    WHERE coalesce((
        SELECT prompt_tokens + completion_tokens FROM api_key_usage_accumulators
        WHERE api_key_id = :key_id AND period_start = :month_start
    ), 0) < :monthly_budget
    ON CONFLICT (api_key_id, window_start) DO UPDATE
    SET requests = api_key_rate_windows.requests + 1
    WHERE api_key_rate_windows.requests < :rate_limit
      AND coalesce((
          SELECT prompt_tokens + completion_tokens FROM api_key_usage_accumulators
          WHERE api_key_id = :key_id AND period_start = :month_start
      ), 0) < :monthly_budget
    RETURNING requests
""").bindparams(
    bindparam("key_id", type_=ApiKeyRow.__table__.c.id.type),
    bindparam("minute_start", type_=RateWindowRow.__table__.c.window_start.type),
    bindparam("minute_end", type_=RateWindowRow.__table__.c.window_end.type),
    bindparam("month_start", type_=UsageAccumulatorRow.__table__.c.period_start.type),
    bindparam("monthly_budget", type_=ApiKeyRow.__table__.c.monthly_budget_tokens.type),
    bindparam("rate_limit", type_=ApiKeyRow.__table__.c.requests_per_minute.type),
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


@dataclass(frozen=True)
class KeyAdmissionWindow:
    """One attempt's counter windows; current key limits are never cached."""

    minute_start: datetime
    minute_end: datetime
    month_start: datetime
    month_end: datetime

    @classmethod
    def current(cls) -> KeyAdmissionWindow:
        minute_start, minute_end = minute_window()
        month_start, month_end = month_window()
        return cls(minute_start, minute_end, month_start, month_end)

    def parameters(self, key: ApiKeyRow) -> dict[str, object]:
        return {
            "key_id": key.id,
            "minute_start": self.minute_start,
            "minute_end": self.minute_end,
            "month_start": self.month_start,
            "monthly_budget": key.monthly_budget_tokens,
            "rate_limit": key.requests_per_minute,
        }


def locked_key_admission_cte(name: str) -> CTE:
    """Compose the same atomic admission into a dependent database write."""
    return _LOCKED_KEY_ADMISSION.columns(requests=Integer).cte(name)


async def enforce_locked_limits(session: AsyncSession, key: ApiKeyRow) -> None:
    """Admit using the current key's mutation lock and fresh live counters."""
    window = KeyAdmissionWindow.current()
    if await session.scalar(_LOCKED_KEY_ADMISSION, window.parameters(key)) is not None:
        return
    await raise_locked_limit_refusal(session, key, window)


async def raise_locked_limit_refusal(
    session: AsyncSession, key: ApiKeyRow, window: KeyAdmissionWindow
) -> NoReturn:
    """Classify an atomic refusal without retrying admission or changing counters."""
    minute_start, minute_end = window.minute_start, window.minute_end
    month_start, month_end = window.month_start, window.month_end
    consumed = func.coalesce(
        select(UsageAccumulatorRow.prompt_tokens + UsageAccumulatorRow.completion_tokens)
        .where(
            UsageAccumulatorRow.api_key_id == key.id,
            UsageAccumulatorRow.period_start == month_start,
        )
        .scalar_subquery(),
        0,
    )
    # Preserve rate-first refusal precedence when both limits are exhausted.
    admitted = await session.scalar(
        select(RateWindowRow.requests).where(
            RateWindowRow.api_key_id == key.id, RateWindowRow.window_start == minute_start
        )
    )
    if admitted is not None and admitted >= key.requests_per_minute:
        limit_refusals.add(1, {"kind": "rate"})
        raise RateLimitExceeded(minute_end)
    used = int(await session.scalar(select(consumed)) or 0)
    limit_refusals.add(1, {"kind": "quota"})
    raise MonthlyQuotaExceeded(key.monthly_budget_tokens, used, month_end)


async def enforce_limits(session: AsyncSession, key_id: uuid.UUID) -> ApiKeyRow:
    key = await session.scalar(select(ApiKeyRow).where(ApiKeyRow.id == key_id).with_for_update())
    if key is None or key.revoked_at is not None:
        raise LookupError("active API key not found")
    await admit_rate(session, key.id, key.requests_per_minute)
    await check_quota(session, key)
    return key
