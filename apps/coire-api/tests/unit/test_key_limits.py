from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from coire_api.identity.limits import RateLimitExceeded, admit_rate, settle_usage


@pytest.mark.asyncio
async def test_atomic_rate_admission_refuses_when_conditional_upsert_returns_nothing() -> None:
    session = AsyncMock()
    session.scalar.return_value = None
    with pytest.raises(RateLimitExceeded) as exc:
        await admit_rate(session, uuid.uuid4(), 1)
    assert exc.value.retry_at.second == 0


@pytest.mark.asyncio
async def test_usage_settlement_is_one_atomic_upsert() -> None:
    session = AsyncMock()
    await settle_usage(session, uuid.uuid4(), prompt_tokens=10, completion_tokens=5)
    session.execute.assert_awaited_once()
