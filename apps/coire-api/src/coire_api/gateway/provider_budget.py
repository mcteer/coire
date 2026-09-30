"""Conservative, serializable per-model provider token allowance."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ModelRow, ProviderBudgetReservationRow
from coire_api.gateway.resolution import ResolvedModel
from coire_core.models.gateway import ChatMessage
from coire_core.models.registry import ModelSource


class ProviderBudgetExceeded(Exception):
    """The configured daily cap cannot cover a worst-case request."""


def prompt_token_ceiling(messages: list[ChatMessage]) -> int:
    """Conservatively bound tokenizer usage by UTF-8 bytes plus message framing."""
    total = 0
    for message in messages:
        if not isinstance(message.content, str):
            raise ProviderBudgetExceeded("provider accepts text only")
        total += len(message.content.encode("utf-8")) + 32
    return total


async def reserve_provider_budget(
    session: AsyncSession,
    resolved: ResolvedModel,
    messages: list[ChatMessage],
    output_tokens: int,
    request_id: uuid.UUID,
) -> int:
    """Lock the registry row so concurrent reservations see each other's holds."""
    if resolved.source is ModelSource.STUDIO:
        raise ValueError("Studio model cannot use a provider budget")
    hold = prompt_token_ceiling(messages) + output_tokens
    model = await session.get(ModelRow, resolved.model_id, with_for_update=True)
    if model is None or model.daily_token_budget is None or model.daily_token_budget <= 0:
        raise ProviderBudgetExceeded("provider budget unavailable")
    day = datetime.now(UTC).date()
    spent = await session.scalar(
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        ProviderBudgetReservationRow.actual_tokens,
                        ProviderBudgetReservationRow.reserved_tokens,
                    )
                ),
                0,
            )
        ).where(
            ProviderBudgetReservationRow.model_id == resolved.model_id,
            ProviderBudgetReservationRow.day == day,
        )
    )
    if int(spent or 0) + hold > model.daily_token_budget:
        raise ProviderBudgetExceeded("provider daily token budget exhausted")
    session.add(
        ProviderBudgetReservationRow(
            request_id=request_id,
            model_id=resolved.model_id,
            day=day,
            reserved_tokens=hold,
        )
    )
    await session.commit()
    return hold


async def settle_provider_budget(
    session: AsyncSession, request_id: uuid.UUID, *, actual_tokens: int
) -> None:
    """Release unused hold only after verified usage. Unknown billing keeps full hold."""
    reservation = await session.get(ProviderBudgetReservationRow, request_id)
    if reservation is not None:
        reservation.actual_tokens = max(0, actual_tokens)
