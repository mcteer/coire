"""Immediate eligibility tombstones and bounded physical content erasure."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ChatFeedbackProvenanceRow, ComparisonPairRow, FeedbackRow
from coire_api.feedback.telemetry import purge_oldest_seconds, purged_total, tracer


async def withdraw_sources(
    session: AsyncSession, owner: uuid.UUID, *, conversation_id: uuid.UUID | None = None
) -> None:
    """Caller holds the owner preference and then conversation lock when applicable."""
    now = datetime.now(UTC)
    deadline = now + timedelta(hours=24)
    for model in (ChatFeedbackProvenanceRow, ComparisonPairRow, FeedbackRow):
        statement = update(model).where(model.owner_user_id == owner, model.withdrawn_at.is_(None))
        if conversation_id is not None:
            statement = statement.where(model.conversation_id == conversation_id)
        values: dict[str, object] = {"withdrawn_at": now}
        if model is not FeedbackRow:
            values["purge_after"] = deadline
        if model is ComparisonPairRow:
            values.update(generation_state="withdrawn", selection_state="withdrawn")
        await session.execute(statement.values(**values))


async def purge_sources(session: AsyncSession, *, batch_size: int = 100) -> int:
    """Erase copied content as soon as withdrawn, without deleting immutable memberships."""
    if not 1 <= batch_size <= 100:
        raise ValueError("feedback purge batch must be 1..100")
    now = datetime.now(UTC)
    count = 0
    with tracer.start_as_current_span(
        "coire.api.feedback.purge", record_exception=False, set_status_on_exception=False
    ):
        for model in (ComparisonPairRow, ChatFeedbackProvenanceRow):
            rows = (
                await session.scalars(
                    select(model)
                    .where(model.purged_at.is_(None), model.withdrawn_at.is_not(None))
                    .order_by(model.purge_after, model.id)
                    .limit(batch_size - count)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                assert isinstance(row, (ComparisonPairRow, ChatFeedbackProvenanceRow))
                row.prompt = None
                if isinstance(row, ComparisonPairRow):
                    row.original = None
                    row.candidate = None
                    row.execution = {}
                # Body removal and logical-byte release commit together.
                row.counted_bytes = 0
                row.purged_at = now
                count += 1
            if count >= batch_size:
                break
        if count < batch_size:
            feedback = list(
                await session.scalars(
                    select(FeedbackRow)
                    .where(FeedbackRow.withdrawn_at.is_not(None), FeedbackRow.purged_at.is_(None))
                    .order_by(FeedbackRow.withdrawn_at, FeedbackRow.id)
                    .limit(batch_size - count)
                    .with_for_update(skip_locked=True)
                )
            )
            for item in feedback:
                item.judgement = None
                item.tags = []
                item.purged_at = now
                count += 1
        await session.flush()
    purged_total.add(count)
    return count


async def expire_sources(session: AsyncSession, *, batch_size: int = 100) -> int:
    """Unused snapshots and pending comparisons cannot retain capture bodies forever."""
    from coire_api.db import ChatConversationRow, UserRow

    if not 1 <= batch_size <= 100:
        raise ValueError("feedback expiry batch must be 1..100")
    now = datetime.now(UTC)
    lease = ComparisonPairRow.execution["lease_expires_at"].as_string()
    pending = (
        await session.scalars(
            select(ComparisonPairRow.id)
            .where(
                ComparisonPairRow.selection_state == "pending",
                or_(
                    ComparisonPairRow.expires_at <= now,
                    and_(
                        ComparisonPairRow.generation_state.in_(("queued", "running")),
                        or_(
                            lease <= now.isoformat(),
                            and_(
                                lease.is_(None),
                                ComparisonPairRow.created_at <= now - timedelta(seconds=10),
                            ),
                        ),
                    ),
                ),
            )
            .order_by(ComparisonPairRow.owner_user_id, ComparisonPairRow.id)
            .limit(batch_size)
        )
    ).all()
    count = 0
    for identity in pending:
        # Discovery takes no row lock. Mutations acquire owner before pair.
        pair = await session.get(ComparisonPairRow, identity)
        if pair is None:
            continue
        await session.get(UserRow, pair.owner_user_id, with_for_update=True, populate_existing=True)
        conversation = await session.get(
            ChatConversationRow, pair.conversation_id, with_for_update=True, populate_existing=True
        )
        pair = await session.get(
            ComparisonPairRow, identity, with_for_update=True, populate_existing=True
        )
        if pair is None or pair.selection_state != "pending":
            continue
        expired = pair.expires_at <= now
        lost = False
        if pair.generation_state in {"queued", "running"}:
            raw_lease = pair.execution.get("lease_expires_at")
            if isinstance(raw_lease, str):
                try:
                    deadline = datetime.fromisoformat(raw_lease)
                    lost = deadline.tzinfo is None or deadline <= now
                except ValueError:
                    lost = True
            else:
                lost = pair.created_at <= now - timedelta(seconds=10)
        if not expired and not lost:
            continue
        pair.generation_state = "expired" if expired else "failed"
        pair.selection_state = "expired" if expired else "dismissed"
        pair.withdrawn_at = now
        pair.purge_after = now + timedelta(hours=24)
        pair.version += 1
        await session.execute(
            update(FeedbackRow)
            .where(FeedbackRow.pair_id == pair.id, FeedbackRow.withdrawn_at.is_(None))
            .values(withdrawn_at=now)
        )
        if conversation is not None and conversation.deleted_at is None:
            from coire_api.feedback.comparisons import pair_event

            await pair_event(session, conversation, pair, "comparison.terminal")
        count += 1
    if count < batch_size:
        expired_query = (
            select(ChatFeedbackProvenanceRow.id)
            .where(
                ChatFeedbackProvenanceRow.expires_at <= now,
                ChatFeedbackProvenanceRow.withdrawn_at.is_(None),
            )
            .order_by(ChatFeedbackProvenanceRow.expires_at, ChatFeedbackProvenanceRow.id)
            .limit(batch_size - count)
            .with_for_update(skip_locked=True)
        )
        identities = list(await session.scalars(expired_query))
        if identities:
            await session.execute(
                update(ChatFeedbackProvenanceRow)
                .where(ChatFeedbackProvenanceRow.id.in_(identities))
                .values(withdrawn_at=now, purge_after=now + timedelta(hours=24))
            )
            count += len(identities)
    await session.flush()
    return count


async def record_purge_age(session: AsyncSession) -> float:
    now = datetime.now(UTC)
    age = 0.0
    for model in (ComparisonPairRow, ChatFeedbackProvenanceRow, FeedbackRow):
        oldest = await session.scalar(
            select(func.min(model.withdrawn_at)).where(model.purged_at.is_(None))
        )
        if oldest is not None:
            age = max(age, (now - oldest).total_seconds())
    purge_oldest_seconds.set(age)
    return age
