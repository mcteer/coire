"""Versioned owner mutations; replay always projects fresh privacy state."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Literal, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    ChatFeedbackProvenanceRow,
    ChatMessageRow,
    ChatTurnRow,
    ComparisonPairRow,
    FeedbackMutationRow,
    FeedbackPreferenceRow,
    FeedbackRow,
)
from coire_api.feedback.eligibility import authorize_owner, lock_conversation, lock_preference
from coire_api.feedback.provenance import comparison_eligibility
from coire_api.feedback.retention import withdraw_sources
from coire_api.feedback.telemetry import mutations_total, tracer
from coire_core.errors import FeedbackConflict, FeedbackForbidden, FeedbackNotFound
from coire_core.models.feedback import (
    ConversationFeedbackPage,
    FeedbackPreference,
    FeedbackPreferenceUpdate,
    FeedbackReceipt,
    MessageFeedback,
    ThumbUpdate,
)
from coire_core.models.preference import canonical_bytes


def preference_detail(row: FeedbackPreferenceRow) -> FeedbackPreference:
    return FeedbackPreference(
        owner_id=row.owner_user_id,
        enabled=row.enabled,
        capture_generation=row.capture_generation,
        version=row.version,
        changed_at=row.changed_at,
        withdrawn_at=row.withdrawn_at,
    )


async def read_preference(session: AsyncSession, principal: Principal) -> FeedbackPreference:
    owner = await authorize_owner(session, principal)
    row = await lock_preference(session, owner)
    return preference_detail(row)


async def change_preference(
    session: AsyncSession, principal: Principal, body: FeedbackPreferenceUpdate
) -> FeedbackPreference:
    with tracer.start_as_current_span(
        "coire.api.feedback.preference", record_exception=False, set_status_on_exception=False
    ):
        owner = await authorize_owner(session, principal)
        row = await lock_preference(session, owner)
        intent = hashlib.sha256(canonical_bytes(body.model_dump(mode="json"))).hexdigest()
        receipt = await session.scalar(
            select(FeedbackMutationRow).where(
                FeedbackMutationRow.actor_user_id == owner,
                FeedbackMutationRow.operation == "preference",
                FeedbackMutationRow.request_id == str(body.client_request_id),
            )
        )
        if receipt is not None:
            if receipt.intent_sha256 != intent:
                raise FeedbackConflict("Request identity was reused with different input")
            return preference_detail(row)
        if row.version != body.expected_version:
            raise FeedbackConflict("Feedback setting changed; reload before saving")
        row.enabled = body.enabled
        row.capture_generation += 1
        row.version += 1
        row.changed_at = datetime.now(UTC)
        row.disclosure_version = body.disclosure_version
        # Every settings mutation fences old writers, even an enabled -> enabled replay
        # with a new command identity. Prior content cannot survive a generation change.
        row.withdrawn_at = row.changed_at
        await withdraw_sources(session, owner)
        session.add(
            FeedbackMutationRow(
                actor_user_id=owner,
                operation="preference",
                request_id=str(body.client_request_id),
                intent_sha256=intent,
                receipt={"owner_id": str(owner), "version": row.version},
            )
        )
        await write_principal_audit(
            session,
            principal=principal,
            action="feedback.preference",
            target_type="user",
            target_id=str(owner),
            context={"enabled": row.enabled, "generation": row.capture_generation},
        )
        await session.flush()
        mutations_total.add(1, {"operation": "preference", "outcome": "accepted"})
        return preference_detail(row)


def thumb_receipt(row: FeedbackRow) -> FeedbackReceipt:
    return FeedbackReceipt(
        id=row.id,
        version=row.version,
        judgement=cast("Literal['up', 'down'] | None", row.judgement),
        source="owner",
    )


async def change_thumb(
    session: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    body: ThumbUpdate,
) -> FeedbackReceipt:
    owner = await authorize_owner(session, principal)
    preference = await lock_preference(session, owner)
    await lock_conversation(session, owner, conversation_id)
    message = await session.get(ChatMessageRow, message_id)
    if message is None or message.conversation_id != conversation_id or message.role != "assistant":
        raise FeedbackNotFound()
    if not preference.enabled:
        raise FeedbackForbidden("Feedback capture is disabled")
    turn = await session.scalar(
        select(ChatTurnRow).where(ChatTurnRow.assistant_message_id == message_id)
    )
    if turn is not None and turn.state != "completed":
        raise FeedbackConflict("Feedback requires a completed answer")
    operation = "thumb"
    intent = hashlib.sha256(
        canonical_bytes(
            {
                "conversation_id": str(conversation_id),
                "message_id": str(message_id),
                "body": body.model_dump(mode="json"),
            }
        )
    ).hexdigest()
    receipt = await session.scalar(
        select(FeedbackMutationRow).where(
            FeedbackMutationRow.actor_user_id == owner,
            FeedbackMutationRow.operation == operation,
            FeedbackMutationRow.request_id == str(body.client_request_id),
        )
    )
    row = await session.scalar(
        select(FeedbackRow)
        .where(
            FeedbackRow.owner_user_id == owner,
            FeedbackRow.message_id == message_id,
            FeedbackRow.kind == "thumb",
        )
        .with_for_update()
    )
    if receipt is not None:
        if receipt.intent_sha256 != intent:
            raise FeedbackConflict("Request identity was reused with different input")
        if (
            row is None
            or row.withdrawn_at is not None
            or row.capture_generation != preference.capture_generation
            or receipt.receipt.get("capture_generation") != preference.capture_generation
        ):
            raise FeedbackForbidden("This contribution was withdrawn")
        return thumb_receipt(row)
    visible_version = (
        row.version
        if row is not None
        and row.withdrawn_at is None
        and row.capture_generation == preference.capture_generation
        else 0
    )
    if visible_version != body.expected_version:
        raise FeedbackConflict("Feedback changed; reload before saving")
    if row is None:
        row = FeedbackRow(
            owner_user_id=owner,
            actor_user_id=owner,
            conversation_id=conversation_id,
            message_id=message_id,
            kind="thumb",
            source="owner",
            version=1,
            capture_generation=preference.capture_generation,
        )
        session.add(row)
    else:
        row.version += 1
    row.judgement = body.judgement
    row.tags = list(body.tags)
    row.capture_generation = preference.capture_generation
    row.withdrawn_at = None
    row.purged_at = None
    row.updated_at = datetime.now(UTC)
    await session.flush()
    session.add(
        FeedbackMutationRow(
            actor_user_id=owner,
            operation=operation,
            request_id=str(body.client_request_id),
            intent_sha256=intent,
            receipt={
                "feedback_id": str(row.id),
                "capture_generation": preference.capture_generation,
            },
        )
    )
    await write_principal_audit(
        session,
        principal=principal,
        action="feedback.thumb",
        target_type="feedback",
        target_id=str(row.id),
        context={"version": row.version},
    )
    mutations_total.add(1, {"operation": operation, "outcome": "accepted"})
    return thumb_receipt(row)


async def conversation_feedback(
    session: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    *,
    limit: int,
    after_position: int | None,
) -> ConversationFeedbackPage:
    owner = await authorize_owner(session, principal)
    preference = await lock_preference(session, owner)
    conversation = await lock_conversation(session, owner, conversation_id)
    query = select(ChatMessageRow).where(
        ChatMessageRow.conversation_id == conversation_id, ChatMessageRow.role == "assistant"
    )
    if after_position is not None:
        query = query.where(ChatMessageRow.position > after_position)
    messages = list(await session.scalars(query.order_by(ChatMessageRow.position).limit(limit + 1)))
    visible = messages[:limit]
    message_ids = [message.id for message in visible]
    provenances = (
        list(
            await session.scalars(
                select(ChatFeedbackProvenanceRow).where(
                    ChatFeedbackProvenanceRow.owner_user_id == owner,
                    ChatFeedbackProvenanceRow.conversation_id == conversation_id,
                    ChatFeedbackProvenanceRow.source_message_id.in_(message_ids),
                )
            )
        )
        if visible
        else []
    )
    turns = (
        list(
            await session.scalars(
                select(ChatTurnRow).where(
                    ChatTurnRow.conversation_id == conversation_id,
                    ChatTurnRow.assistant_message_id.in_(message_ids),
                )
            )
        )
        if visible
        else []
    )
    latest_position = int(
        await session.scalar(
            select(func.max(ChatMessageRow.position)).where(
                ChatMessageRow.conversation_id == conversation_id,
            )
        )
        or 0
    )
    by_provenance = {row.source_message_id: row for row in provenances}
    by_turn = {row.assistant_message_id: row for row in turns}
    feedback = (
        list(
            await session.scalars(
                select(FeedbackRow).where(
                    FeedbackRow.owner_user_id == owner,
                    FeedbackRow.message_id.in_([message.id for message in visible]),
                    FeedbackRow.kind == "thumb",
                    FeedbackRow.withdrawn_at.is_(None),
                    FeedbackRow.capture_generation == preference.capture_generation,
                )
            )
        )
        if preference.enabled and visible
        else []
    )
    by_message = {row.message_id: row for row in feedback}
    from coire_api.feedback.comparisons import pair_receipt

    pairs = list(
        await session.scalars(
            select(ComparisonPairRow)
            .where(
                ComparisonPairRow.conversation_id == conversation_id,
                ComparisonPairRow.owner_user_id == owner,
            )
            .order_by(ComparisonPairRow.created_at.desc(), ComparisonPairRow.id.desc())
            .limit(100)
        )
    )
    return ConversationFeedbackPage(
        comparisons=[pair_receipt(pair) for pair in pairs],
        items=[
            MessageFeedback(
                message_id=message.id,
                feedback=thumb_receipt(by_message[message.id])
                if message.id in by_message
                else None,
                tags=list(by_message[message.id].tags) if message.id in by_message else [],
                eligibility=comparison_eligibility(
                    preference,
                    conversation,
                    message,
                    by_provenance.get(message.id),
                    by_turn.get(message.id),
                    latest_position,
                ),
            )
            for message in visible
        ],
        next_cursor=str(visible[-1].position) if len(messages) > limit else None,
    )
