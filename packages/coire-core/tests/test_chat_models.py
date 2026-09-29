"""Contracts for canonical chat content and native conversation transport."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from coire_core.models.chat import (
    ChatConversation,
    ChatConversationCreate,
    ChatEvent,
    ChatMessageDelta,
    ChatTurnCreate,
    ChatTurnDetail,
    ChatTurnStatus,
)
from coire_core.models.conversation import Conversation, ConversationMessage, TextPart


def test_canonical_conversation_is_ordered_and_strict() -> None:
    message = ConversationMessage(id=uuid4(), role="user", parts=[TextPart(text="hello")])
    conversation = Conversation(id=uuid4(), messages=[message])
    assert conversation.messages[0].parts[0].text == "hello"
    with pytest.raises(ValidationError):
        TextPart.model_validate({"type": "text", "text": "hello", "path": "/tmp/private"})
    with pytest.raises(ValidationError):
        ConversationMessage.model_validate({"id": str(uuid4()), "role": "user", "parts": []})


def test_native_chat_owner_is_server_supplied_and_revision_bounded() -> None:
    owner_id = uuid4()
    conversation = ChatConversation(
        id=uuid4(),
        owner_id=owner_id,
        title="Hello",
        revision=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert conversation.owner_id == owner_id
    with pytest.raises(ValidationError):
        ChatConversationCreate.model_validate({"owner_id": str(owner_id)})
    with pytest.raises(ValidationError):
        ChatConversation.model_validate({**conversation.model_dump(), "revision": 0})


def test_turn_create_rejects_unbounded_or_ambiguous_selection() -> None:
    request = ChatTurnCreate(
        client_request_id=uuid4(), expected_revision=1, model_id=uuid4(), content="hi"
    )
    assert request.attachments == []
    with pytest.raises(ValidationError):
        ChatTurnCreate.model_validate({**request.model_dump(), "content": "x" * 65537})
    with pytest.raises(ValidationError):
        ChatTurnCreate.model_validate({**request.model_dump(), "content": "é" * 40_000})
    with pytest.raises(ValidationError):
        ChatTurnCreate.model_validate(
            {
                **request.model_dump(),
                "attachments": [{"file_id": str(uuid4()), "mode": "visual", "pages": [1, 1]}],
            }
        )
    with pytest.raises(ValidationError):
        ChatTurnCreate.model_validate({**request.model_dump(), "owner_id": str(uuid4())})
    with pytest.raises(ValidationError):
        ChatTurnCreate.model_validate({**request.model_dump(), "recovery_mode": "continue"})
    with pytest.raises(ValidationError):
        ChatTurnCreate.model_validate({**request.model_dump(), "retry_of": str(uuid4())})


def test_turn_detail_requires_saved_messages_and_cursor() -> None:
    with pytest.raises(ValidationError):
        ChatTurnDetail.model_validate({"event_cursor": 0})


def test_event_payload_is_discriminated_and_cursor_is_scoped() -> None:
    conversation_id = uuid4()
    event = ChatEvent(
        conversation_id=conversation_id,
        cursor=2,
        turn_id=uuid4(),
        created_at=datetime.now(UTC),
        payload=ChatMessageDelta(message_id=uuid4(), channel="answer", text="hi", offset=2),
    )
    assert event.event_id == f"{conversation_id}:2"
    assert event.payload.type == "message.delta"
    with pytest.raises(ValidationError):
        ChatEvent.model_validate({**event.model_dump(), "payload": {"type": "message.delta"}})
    with pytest.raises(ValidationError):
        ChatTurnStatus(state="running", estimate_seconds=-1)
