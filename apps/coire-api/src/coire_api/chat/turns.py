"""Locked text-turn admission and safe persisted projections."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.chat.maintenance import LEASE_SECONDS, PROCESS_ID
from coire_api.chat.text_context import compose_text_prompt
from coire_api.db import (
    ChatConversationRow,
    ChatEventRow,
    ChatMessageRow,
    ChatTurnRow,
    ModelRow,
)
from coire_api.gateway.context import ContextLengthError, enforce_context
from coire_api.registry.service import chat_model_eligible
from coire_core.errors import ChatConflict, ChatContextExceeded, ChatNotFound
from coire_core.models.chat import (
    ChatEvent,
    ChatMessage,
    ChatStopRequest,
    ChatTurn,
    ChatTurnAccepted,
    ChatTurnCreate,
    ChatTurnDetail,
    ChatTurnStatus,
    ChatUsage,
)
from coire_core.models.files import ChatAttachmentSelection
from coire_core.models.gateway import ChatMessage as GatewayMessage
from coire_core.settings import Settings


@dataclass(frozen=True, slots=True)
class Admission:
    turn: ChatTurnRow
    event: ChatEvent | None
    history: list[GatewayMessage]
    prompt_tokens: int
    replay: bool
    output_tokens: int = 1024


def request_hash(body: ChatTurnCreate) -> str:
    return hashlib.sha256(body.model_dump_json(exclude_none=True).encode()).hexdigest()


def project_message(row: ChatMessageRow) -> ChatMessage:
    selections = [
        ChatAttachmentSelection.model_validate(value) for value in row.attachment_selections or []
    ]
    return ChatMessage(
        id=row.id,
        conversation_id=row.conversation_id,
        position=row.position,
        role=cast(Literal["user", "assistant"], row.role),
        text=row.text,
        reasoning=row.reasoning,
        model_id=row.model_id,
        model_display_name=row.model_display_name,
        attachment_ids=[uuid.UUID(value) for value in row.attachment_ids or []],
        attachment_selections=selections,
        created_at=row.created_at,
    )


def project_turn(row: ChatTurnRow) -> ChatTurn:
    usage = ChatUsage.model_validate(row.usage) if row.usage else None
    return ChatTurn(
        id=row.id,
        conversation_id=row.conversation_id,
        client_request_id=row.client_request_id,
        accepted_revision=row.accepted_revision,
        input_message_id=row.input_message_id,
        assistant_message_id=row.assistant_message_id,
        model_id=row.model_id,
        model_display_name=row.model_display_name,
        state=cast(
            Literal[
                "accepted",
                "queued",
                "loading",
                "running",
                "stop_requested",
                "completed",
                "failed",
                "stopped",
                "interrupted",
            ],
            row.state,
        ),
        action=cast(Literal["chat", "research", "plan", "apply"], row.action),
        retry_of=row.retry_of,
        usage=usage,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def read_turn_detail(
    session: AsyncSession, principal: Principal, conversation_id: uuid.UUID, turn_id: uuid.UUID
) -> ChatTurnDetail:
    from coire_api.auth import require_owned_chat

    conversation = await require_owned_chat(session, conversation_id, principal)
    row = await session.get(ChatTurnRow, turn_id)
    if row is None or row.conversation_id != conversation.id:
        raise ChatNotFound()
    input_row = await session.get(ChatMessageRow, row.input_message_id)
    answer_row = await session.get(ChatMessageRow, row.assistant_message_id)
    if input_row is None or answer_row is None:
        raise ChatNotFound()
    return ChatTurnDetail(
        turn=project_turn(row),
        input_message=project_message(input_row),
        assistant_message=project_message(answer_row),
        event_cursor=conversation.event_cursor,
    )


async def request_turn_stop(
    session: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
    body: ChatStopRequest,
    settings: Settings,
) -> ChatTurn:
    """Persist an owner Stop request; the controlling stream performs cancellation."""
    conversation = await session.scalar(
        select(ChatConversationRow)
        .where(
            ChatConversationRow.id == conversation_id,
            ChatConversationRow.owner_user_id == principal.user_id,
            ChatConversationRow.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if conversation is None:
        raise ChatNotFound()
    turn = await session.get(ChatTurnRow, turn_id)
    if turn is None or turn.conversation_id != conversation_id:
        raise ChatNotFound()
    if turn.action != "chat":
        raise ChatConflict("code turn Stop requires the coding run path")
    if turn.state in {"completed", "failed", "stopped", "interrupted", "stop_requested"}:
        return project_turn(turn)
    now = datetime.now(UTC)
    turn.state = "stop_requested"
    turn.stop_reason = body.reason
    turn.updated_at = now
    conversation.event_cursor += 1
    conversation.updated_at = now
    event = ChatEvent(
        conversation_id=conversation_id,
        cursor=conversation.event_cursor,
        turn_id=turn_id,
        created_at=now,
        payload=ChatTurnStatus(state="stop_requested"),
    )
    session.add(
        ChatEventRow(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            turn_id=turn_id,
            cursor=event.cursor,
            type=event.payload.type,
            payload=event.payload.model_dump(mode="json"),
            created_at=now,
            expires_at=now + timedelta(hours=settings.chat_event_retention_hours),
        )
    )
    await session.commit()
    return project_turn(turn)


async def admit_turn(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    principal: Principal,
    body: ChatTurnCreate,
    settings: Settings,
) -> Admission:
    """Commit admission before generation; never hold this transaction during engine I/O."""
    conversation = await session.scalar(
        select(ChatConversationRow)
        .where(ChatConversationRow.id == conversation_id)
        .with_for_update()
    )
    if (
        conversation is None
        or conversation.id != conversation_id
        or conversation.owner_user_id != principal.user_id
        or conversation.deleted_at is not None
    ):
        raise ChatNotFound()

    digest = request_hash(body)
    existing = await session.scalar(
        select(ChatTurnRow).where(
            ChatTurnRow.conversation_id == conversation_id,
            ChatTurnRow.client_request_id == body.client_request_id,
        )
    )
    if existing is not None:
        if existing.request_hash != digest:
            raise ChatConflict("request ID already used with different content")
        return Admission(existing, None, [], 0, True)
    if conversation.revision != body.expected_revision:
        raise ChatConflict("conversation changed; reload before sending")
    if conversation.active_turn_id is not None:
        raise ChatConflict("a turn is already active")
    if body.action != "chat":
        raise ChatConflict("this conversation cannot accept that action yet")
    if body.workspace_id or body.source_revision or body.plan_id or body.research_id:
        raise ChatConflict("coding inputs require code mode")
    if conversation.mode != "chat":
        raise ChatConflict("text chat requires chat mode")

    model = await session.get(ModelRow, body.model_id)
    if model is None or not chat_model_eligible(model, principal):
        raise ChatNotFound()
    saved = (
        (
            await session.execute(
                select(ChatMessageRow)
                .where(ChatMessageRow.conversation_id == conversation_id)
                .order_by(ChatMessageRow.position)
            )
        )
        .scalars()
        .all()
    )
    prior_turns = list(
        (
            await session.execute(
                select(ChatTurnRow).where(ChatTurnRow.conversation_id == conversation_id)
            )
        )
        .scalars()
        .all()
    )
    positions = {message.id: message.position for message in saved}
    latest_by_input: dict[uuid.UUID, uuid.UUID] = {}
    for previous in sorted(
        prior_turns, key=lambda row: positions.get(row.assistant_message_id, -1)
    ):
        latest_by_input[previous.input_message_id] = previous.assistant_message_id
    prior_assistant_ids = {row.assistant_message_id for row in prior_turns}
    selected_assistant_ids = set(latest_by_input.values())
    retry_source: ChatTurnRow | None = None
    source_input: ChatMessageRow | None = None
    if body.retry_of is not None:
        retry_source = await session.get(ChatTurnRow, body.retry_of)
        if (
            retry_source is None
            or retry_source.conversation_id != conversation_id
            or retry_source.action != "chat"
            or retry_source.state not in {"failed", "stopped", "interrupted"}
            or not saved
            or saved[-1].id != retry_source.assistant_message_id
        ):
            raise ChatConflict("only the latest interrupted response can be retried")
        source_input = next(
            (message for message in saved if message.id == retry_source.input_message_id), None
        )
        if (
            source_input is None
            or source_input.role != "user"
            or source_input.text != body.content
            or source_input.attachment_selections
            != [item.model_dump(mode="json") for item in body.attachments]
        ):
            raise ChatConflict("retry must keep the original input and file choices")
    prompt_content = await compose_text_prompt(
        session, principal.user_id, conversation_id, body.content, body.attachments
    )
    if source_input is not None and prompt_content != (
        source_input.prompt_content
        if source_input.prompt_content is not None
        else source_input.text
    ):
        raise ChatConflict("retry input changed; upload a new file or send a new turn")
    history = [
        GatewayMessage(
            role=cast(Literal["user", "assistant"], message.role),
            content=message.prompt_content if message.prompt_content is not None else message.text,
        )
        for message in saved
        if source_input is None or message.position < source_input.position
        if (
            message.role != "assistant"
            or message.id not in prior_assistant_ids
            or message.id in selected_assistant_ids
        )
    ]
    history.append(GatewayMessage(role="user", content=prompt_content))
    output_tokens = (
        min(settings.chat_output_tokens, max(1, model.context_window // 4))
        if model.context_window is not None
        else settings.chat_output_tokens
    )
    try:
        prompt_tokens = enforce_context(
            history, limit=model.context_window, output_tokens=output_tokens
        )
    except ContextLengthError as exc:
        raise ChatContextExceeded(str(exc)) from exc

    now = datetime.now(UTC)
    input_id = source_input.id if source_input is not None else uuid.uuid4()
    assistant_id, turn_id = uuid.uuid4(), uuid.uuid4()
    next_position = saved[-1].position + 1 if saved else 1
    input_row = (
        ChatMessageRow(
            id=input_id,
            conversation_id=conversation_id,
            position=next_position,
            role="user",
            text=body.content,
            prompt_content=prompt_content,
            reasoning="",
            model_id=model.id,
            model_display_name=model.display_name,
            attachment_ids=[str(item.file_id) for item in body.attachments],
            attachment_selections=[item.model_dump(mode="json") for item in body.attachments],
            created_at=now,
        )
        if source_input is None
        else None
    )
    assistant_row = ChatMessageRow(
        id=assistant_id,
        conversation_id=conversation_id,
        position=next_position + (1 if input_row is not None else 0),
        role="assistant",
        text="",
        reasoning="",
        model_id=model.id,
        model_display_name=model.display_name,
        attachment_ids=[],
        created_at=now,
    )
    turn = ChatTurnRow(
        id=turn_id,
        conversation_id=conversation_id,
        client_request_id=body.client_request_id,
        request_hash=digest,
        accepted_revision=body.expected_revision,
        input_message_id=input_id,
        assistant_message_id=assistant_id,
        model_id=model.id,
        model_display_name=model.display_name,
        action="chat",
        retry_of=body.retry_of,
        state="accepted",
        owner_process=PROCESS_ID,
        lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
        event_cursor=conversation.event_cursor + 1,
        created_at=now,
        updated_at=now,
    )
    event = ChatEvent(
        conversation_id=conversation_id,
        cursor=conversation.event_cursor + 1,
        turn_id=turn_id,
        created_at=now,
        payload=ChatTurnAccepted(turn=project_turn(turn)),
    )
    event_row = ChatEventRow(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        turn_id=turn_id,
        cursor=event.cursor,
        type=event.payload.type,
        payload=event.payload.model_dump(mode="json"),
        created_at=now,
        expires_at=now + timedelta(hours=settings.chat_event_retention_hours),
    )
    # There are FKs in both directions between conversation and turn, and no ORM
    # relationships to guide flush ordering. Insert each dependency layer explicitly.
    if input_row is not None:
        session.add(input_row)
    session.add(assistant_row)
    await session.flush()
    session.add(turn)
    await session.flush()
    session.add(event_row)
    await session.flush()
    conversation.active_turn_id = turn_id
    conversation.selected_model_id = model.id
    conversation.revision += 1
    conversation.event_cursor += 1
    conversation.updated_at = now
    await session.commit()
    return Admission(turn, event, history, prompt_tokens, False, output_tokens)
