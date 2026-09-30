from __future__ import annotations

import uuid
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ProviderBudgetReservationRow
from coire_api.gateway.provider_budget import (
    ProviderBudgetExceeded,
    prompt_token_ceiling,
    reserve_provider_budget,
    settle_provider_budget,
)
from coire_api.gateway.resolution import ResolvedModel
from coire_core.models.gateway import ChatMessage
from coire_core.models.registry import ModelSource


def _resolved() -> ResolvedModel:
    return ResolvedModel(
        model_id=uuid.uuid4(),
        slug="openai--example",
        context_window=8192,
        model_path=None,
        engine_id=None,
        node=None,
        engine_url=None,
        source=ModelSource.OPENAI,
        provider_model_id="example",
        max_output_tokens=100,
        daily_token_budget=500,
    )


@pytest.mark.asyncio
async def test_provider_budget_holds_worst_case_and_settles_reported_usage() -> None:
    rows: dict[uuid.UUID, ProviderBudgetReservationRow] = {}

    class Session:
        committed = False

        async def get(self, model: object, identifier: uuid.UUID, **_kwargs: object) -> object:
            if model is ProviderBudgetReservationRow:
                return rows.get(identifier)
            return type("Model", (), {"daily_token_budget": 500})()

        async def scalar(self, _query: object) -> int:
            return sum(
                row.reserved_tokens if row.actual_tokens is None else row.actual_tokens
                for row in rows.values()
            )

        def add(self, row: ProviderBudgetReservationRow) -> None:
            rows[row.request_id] = row

        async def commit(self) -> None:
            self.committed = True

    session = Session()
    resolved = _resolved()
    messages = [ChatMessage(role="user", content="Hello")]
    request_id = uuid.uuid4()
    assert prompt_token_ceiling(messages) == 37
    hold = await reserve_provider_budget(
        cast(AsyncSession, session), resolved, messages, 100, request_id
    )
    assert hold == 137
    assert session.committed
    assert rows[request_id].actual_tokens is None
    await settle_provider_budget(cast(AsyncSession, session), request_id, actual_tokens=12)
    assert rows[request_id].actual_tokens == 12


@pytest.mark.asyncio
async def test_provider_budget_refuses_exhausted_allowance() -> None:
    class Session:
        async def get(self, _model: object, _identifier: uuid.UUID, **_kwargs: object) -> object:
            return type("Model", (), {"daily_token_budget": 100})()

        async def scalar(self, _query: object) -> int:
            return 90

    with pytest.raises(ProviderBudgetExceeded, match="exhausted"):
        await reserve_provider_budget(
            cast(AsyncSession, Session()),
            _resolved(),
            [ChatMessage(role="user", content="Hello")],
            16,
            uuid.uuid4(),
        )
