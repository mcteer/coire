"""Bounded, durable progress replay from an operation's committed cursor."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import UpgradeReceiptRow
from coire_api.upgrades.persistence import get_operation
from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import UpgradeEvent, UpgradeEventPage, UpgradeOperation


async def event_page(
    session: AsyncSession, job_id: str, *, after: int = 0, limit: int = 100
) -> UpgradeEventPage:
    if not 0 <= after <= 2**63 - 1 or not 1 <= limit <= 100:
        raise ValueError("Progress cursor or page size is outside its bounds")
    row = await get_operation(session, job_id)
    operation = UpgradeOperation.model_validate(row.payload)
    reset = after > operation.receipt_sequence
    cursor = 0 if reset else after
    rows = (
        await session.scalars(
            select(UpgradeReceiptRow)
            .where(
                UpgradeReceiptRow.job_id == job_id,
                UpgradeReceiptRow.generation == operation.generation,
                UpgradeReceiptRow.sequence > cursor,
                UpgradeReceiptRow.sequence <= operation.receipt_sequence,
            )
            .order_by(UpgradeReceiptRow.sequence)
            .limit(limit)
        )
    ).all()
    events = tuple(UpgradeEvent.model_validate(item.payload) for item in rows)
    expected_count = min(limit, operation.receipt_sequence - cursor)
    if len(events) != expected_count:
        raise UpgradeUnavailable("Durable progress history is incomplete")
    for expected, stored, event in zip(
        range(cursor + 1, cursor + 1 + len(events)), rows, events, strict=True
    ):
        if (
            event.sequence != expected
            or event.job_id != job_id
            or event.generation != operation.generation
            or event.sha256() != stored.receipt_sha256
        ):
            raise UpgradeUnavailable("Durable progress history failed verification")
    return UpgradeEventPage(
        operation=operation,
        events=events,
        cursor=events[-1].sequence if events else cursor,
        reset=reset,
    )
