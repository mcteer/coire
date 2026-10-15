"""Idempotent reconciliation of non-authoritative Studio failover events."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import FailoverEventReceiptRow
from coire_core.models.audit import AuditOutcome
from coire_core.models.auth import ActorType
from coire_core.models.failover import FailoverEvent


async def reconcile_event(session: AsyncSession, event: FailoverEvent) -> bool:
    """Append an event to the authoritative audit log once; return false on replay."""
    existing = await session.get(FailoverEventReceiptRow, event.event_id)
    if existing is not None:
        if existing.host != event.host or existing.term != event.term:
            raise ValueError("event receipt identity conflicts with replay")
        return False
    try:
        async with session.begin_nested():
            await _insert_event(session, event)
        return True
    except IntegrityError:
        # The savepoint rolls back the losing audit row too; a concurrent winner
        # commits its audit and receipt together before this unique-key conflict.
        receipt = await session.get(FailoverEventReceiptRow, event.event_id)
        if receipt is None or receipt.host != event.host or receipt.term != event.term:
            raise
        return False


async def _insert_event(session: AsyncSession, event: FailoverEvent) -> None:
    audit = await write_audit(
        session,
        actor=event.host,
        actor_type=ActorType.SERVICE,
        action=f"failover.{event.kind.value}",
        target_type="failover",
        target_id=str(event.event_id),
        outcome=AuditOutcome.OK,
        detail={"term": event.term, "proof_digest": event.proof_digest},
        context={"occurred_at": event.occurred_at.isoformat()},
    )
    session.add(
        FailoverEventReceiptRow(
            event_id=event.event_id,
            host=event.host,
            term=event.term,
            audit_id=audit.id,
            reconciled_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def reconcile_events(session: AsyncSession, events: list[FailoverEvent]) -> int:
    """Reconcile a replay batch in stable time/id order within the caller's transaction."""
    added = 0
    for event in sorted(events, key=lambda item: (item.occurred_at, str(item.event_id))):
        added += int(await reconcile_event(session, event))
    return added
