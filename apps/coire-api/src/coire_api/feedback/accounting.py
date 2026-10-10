"""Once-only gateway settlement survives API crashes and contribution erasure."""

from __future__ import annotations

from typing import Literal, cast

from sqlalchemy import select

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ComparisonPairRow, session_scope
from coire_api.gateway.usage import UsageTracker, persist_usage
from coire_core.models.feedback import ComparisonAccounting
from coire_core.models.gateway import GatewayProtocol, UsageOutcome


def snapshot(usage: UsageTracker) -> dict[str, object]:
    assert (
        usage.principal.user_id is not None
        and usage.model_id is not None
        and usage.variant_id is not None
    )
    return ComparisonAccounting(
        request_id=usage.request_id,
        owner_id=usage.principal.user_id,
        principal_kind=cast(Literal["user", "admin", "api_key"], usage.principal.kind.value),
        principal_subject=usage.principal.subject,
        api_key_id=usage.principal.api_key_id,
        model_id=usage.model_id,
        variant_id=usage.variant_id,
        adapter_id=usage.adapter_id,
        engine_id=usage.engine_id,
        instance_id=usage.instance_id,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        started_at=usage.started_at,
        first_token_at=usage.first_token_at,
        first_token_duration_ms=usage.first_token_duration_ms,
        outcome=usage.outcome or UsageOutcome.FAILED,
    ).model_dump(mode="json")


async def settle_comparison(pair_id: str) -> bool:
    # The database's unique usage request ID fences worker/recovery races. No live
    # generation authority is needed to settle work that has already happened.
    async with session_scope() as session:
        pair = await session.get(ComparisonPairRow, pair_id)
        if pair is None or pair.generation_state in {"queued", "running"} or not pair.accounting:
            return False
        receipt = ComparisonAccounting.model_validate(pair.accounting)
        if receipt.settled:
            return False
    principal = Principal(
        kind=PrincipalKind(receipt.principal_kind),
        subject=receipt.principal_subject,
        user_id=receipt.owner_id,
        api_key_id=receipt.api_key_id,
    )
    await persist_usage(
        request_id=receipt.request_id,
        principal=principal,
        requested_model_id=str(receipt.model_id),
        model_id=receipt.model_id,
        engine_id=receipt.engine_id,
        protocol=GatewayProtocol.OPENAI,
        prompt_tokens=receipt.prompt_tokens,
        completion_tokens=receipt.completion_tokens,
        started_at=receipt.started_at,
        outcome=receipt.outcome,
        failure_code=None if receipt.outcome is UsageOutcome.SUCCEEDED else "comparison_ended",
        variant_id=receipt.variant_id,
        adapter_id=receipt.adapter_id,
        instance_id=receipt.instance_id,
        first_token_at=receipt.first_token_at,
        first_token_duration_ms=receipt.first_token_duration_ms,
    )
    async with session_scope() as session:
        pair = await session.get(ComparisonPairRow, pair_id, with_for_update=True)
        if pair is None or not pair.accounting:
            return True
        current = ComparisonAccounting.model_validate(pair.accounting)
        if current.request_id != receipt.request_id:
            raise ValueError("comparison accounting identity changed")
        pair.accounting = current.model_copy(update={"settled": True}).model_dump(mode="json")
        await session.commit()
    return True


async def persist_accounting(pair_id: str, usage: UsageTracker, outcome: UsageOutcome) -> None:
    async with session_scope() as session:
        pair = await session.get(ComparisonPairRow, pair_id, with_for_update=True)
        if pair is None or not pair.accounting:
            return
        current = ComparisonAccounting.model_validate(pair.accounting)
        if current.request_id != usage.request_id or current.settled:
            return
        captured = snapshot(usage)
        captured["outcome"] = outcome.value
        pair.accounting = captured
        await session.commit()


async def settle_comparisons(*, batch_size: int = 100) -> int:
    async with session_scope() as session:
        identities = list(
            await session.scalars(
                select(ComparisonPairRow.id)
                .where(
                    ComparisonPairRow.generation_state.notin_(("queued", "running")),
                    ComparisonPairRow.accounting["settled"].as_boolean().is_(False),
                )
                .order_by(ComparisonPairRow.id)
                .limit(batch_size)
            )
        )
    count = 0
    for pair_id in identities:
        count += int(await settle_comparison(pair_id))
    return count
