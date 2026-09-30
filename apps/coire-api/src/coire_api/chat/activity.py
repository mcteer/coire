"""Persist assigned Studio coding receipts before the run output is removed."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from opentelemetry import metrics, trace
from sqlalchemy import select

from coire_api.db import (
    AgentRunRow,
    ChatConversationRow,
    ChatEventRow,
    ChatTurnRow,
    session_scope,
)
from coire_api.nodes_client import NodeClient
from coire_core.models.chat import ChatEvent, ChatRunActivity, ChatRunActivityStatus
from coire_core.models.runs import RunActivityPage
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.chat.activity")
activity_total = metrics.get_meter("coire.api.chat.activity").create_counter(
    "coire_chat_run_activity_total", unit="1"
)


async def activity_cursor(run_id: uuid.UUID) -> int | None:
    """Return None for a plain MCP run without a Chat coding turn."""
    async with session_scope() as session:
        cursor: int | None = await session.scalar(
            select(ChatTurnRow.activity_sequence).where(ChatTurnRow.run_id == run_id).limit(1)
        )
        return cursor


async def persist_activity_page(
    run_id: uuid.UUID, page: RunActivityPage, settings: Settings
) -> int:
    """Deduplicate and append a page under the owner conversation lock."""
    if page.run_id != run_id:
        raise ValueError("activity page run identity mismatch")
    async with session_scope() as session:
        turn_ref = await session.scalar(
            select(ChatTurnRow).where(ChatTurnRow.run_id == run_id).limit(1)
        )
        if turn_ref is None:
            return 0
        conversation = await session.scalar(
            select(ChatConversationRow)
            .where(ChatConversationRow.id == turn_ref.conversation_id)
            .with_for_update()
        )
        if conversation is None or conversation.deleted_at is not None:
            return 0
        turn = await session.scalar(
            select(ChatTurnRow).where(ChatTurnRow.id == turn_ref.id).with_for_update()
        )
        run = await session.get(AgentRunRow, run_id)
        if (
            turn is None
            or turn.run_id != run_id
            or turn.action not in {"research", "plan", "apply"}
            or turn.coding_call_id is None
            or run is None
            or run.prepared_request_id != turn.coding_call_id
            or run.requester_user_id != conversation.owner_user_id
        ):
            raise ValueError("activity run is not owned by this coding turn")
        if turn.activity_final_state is not None:
            return 0
        saved = 0
        expected = turn.activity_sequence + 1
        for receipt in page.data:
            if receipt.run_id != run_id:
                raise ValueError("activity receipt run identity mismatch")
            if receipt.sequence < expected:
                continue
            if receipt.sequence != expected:
                raise ValueError("activity sequence is not contiguous")
            now = datetime.now(UTC)
            conversation.event_cursor += 1
            event = ChatEvent(
                conversation_id=conversation.id,
                cursor=conversation.event_cursor,
                turn_id=turn.id,
                created_at=now,
                payload=ChatRunActivity(activity=receipt),
            )
            session.add(
                ChatEventRow(
                    id=uuid.uuid4(),
                    conversation_id=conversation.id,
                    turn_id=turn.id,
                    cursor=event.cursor,
                    type=event.payload.type,
                    payload=event.payload.model_dump(mode="json"),
                    created_at=now,
                    expires_at=now + timedelta(hours=settings.chat_event_retention_hours),
                )
            )
            turn.activity_sequence = receipt.sequence
            turn.updated_at = now
            expected += 1
            saved += 1
        return saved


async def persist_activity_final_status(
    run_id: uuid.UUID,
    state: Literal["complete", "truncated", "unavailable"],
    settings: Settings,
) -> bool:
    """Record one owner-visible final spool state across collector retries."""
    async with session_scope() as session:
        turn_ref = await session.scalar(
            select(ChatTurnRow).where(ChatTurnRow.run_id == run_id).limit(1)
        )
        if turn_ref is None:
            return False
        conversation = await session.scalar(
            select(ChatConversationRow)
            .where(ChatConversationRow.id == turn_ref.conversation_id)
            .with_for_update()
        )
        if conversation is None or conversation.deleted_at is not None:
            return False
        turn = await session.scalar(
            select(ChatTurnRow).where(ChatTurnRow.id == turn_ref.id).with_for_update()
        )
        run = await session.get(AgentRunRow, run_id)
        if (
            turn is None
            or turn.run_id != run_id
            or turn.action not in {"research", "plan", "apply"}
            or turn.coding_call_id is None
            or run is None
            or run.prepared_request_id != turn.coding_call_id
            or run.requester_user_id != conversation.owner_user_id
        ):
            raise ValueError("activity run is not owned by this coding turn")
        if turn.activity_final_state is not None:
            return False
        now = datetime.now(UTC)
        conversation.event_cursor += 1
        payload = ChatRunActivityStatus(
            run_id=run_id, state=state, last_sequence=turn.activity_sequence
        )
        session.add(
            ChatEventRow(
                id=uuid.uuid4(),
                conversation_id=conversation.id,
                turn_id=turn.id,
                cursor=conversation.event_cursor,
                type=payload.type,
                payload=payload.model_dump(mode="json"),
                created_at=now,
                expires_at=now + timedelta(hours=settings.chat_event_retention_hours),
            )
        )
        turn.activity_final_state = state
        turn.updated_at = now
        return True


async def collect_run_activity(
    run_id: uuid.UUID, node_name: str, settings: Settings, *, final: bool = False
) -> int:
    """Resume from the DB cursor and drain every available bounded node page."""
    cursor = await activity_cursor(run_id)
    if cursor is None:
        return 0
    saved = 0
    with tracer.start_as_current_span("coire.api.chat.run_activity.collect") as span:
        span.set_attribute("run_id", str(run_id))
        try:
            async with NodeClient(settings) as client:
                while True:
                    page = await client.run_activity(node_name, run_id, after_sequence=cursor)
                    if not page.available:
                        if final:
                            await persist_activity_final_status(run_id, "unavailable", settings)
                        activity_total.add(
                            1, {"outcome": "unavailable", "final": str(final).lower()}
                        )
                        return saved
                    saved += await persist_activity_page(run_id, page, settings)
                    if page.data:
                        cursor = page.data[-1].sequence
                    if page.next_sequence is None:
                        if final:
                            await persist_activity_final_status(
                                run_id, "truncated" if page.truncated else "complete", settings
                            )
                        activity_total.add(
                            1,
                            {
                                "outcome": "truncated" if page.truncated else "collected",
                                "final": str(final).lower(),
                            },
                        )
                        return saved
        except Exception as exc:
            activity_total.add(1, {"outcome": "failed", "final": str(final).lower()})
            logger.error(
                "chat activity collection failed run_id=%s error_type=%s",
                run_id,
                type(exc).__name__,
            )
            raise
