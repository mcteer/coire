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
    ChatRunActivity,
    ChatRunActivityStatus,
    ChatTurn,
    ChatTurnCreate,
    ChatTurnDetail,
    ChatTurnResult,
    ChatTurnStatus,
)
from coire_core.models.conversation import Conversation, ConversationMessage, TextPart
from coire_core.models.runs import RunActivity, RunActivityTool


def test_canonical_conversation_is_ordered_and_strict() -> None:
    message = ConversationMessage(id=uuid4(), role="user", parts=[TextPart(text="hello")])
    conversation = Conversation(id=uuid4(), messages=[message])
    assert isinstance(conversation.messages[0].parts[0], TextPart)
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


def test_code_turn_exposes_only_its_bound_call_identity() -> None:
    call_id = uuid4()
    turn = ChatTurn(
        id=uuid4(),
        conversation_id=uuid4(),
        client_request_id=uuid4(),
        accepted_revision=1,
        input_message_id=uuid4(),
        assistant_message_id=uuid4(),
        model_id=uuid4(),
        model_display_name="Coding model",
        state="completed",
        action="plan",
        coding_call_id=call_id,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert ChatTurn.model_validate_json(turn.model_dump_json()).coding_call_id == call_id
    with pytest.raises(ValidationError):
        ChatTurn.model_validate({**turn.model_dump(), "run_token": "secret"})


def test_chat_activity_event_is_strict_and_content_free() -> None:
    run_id = uuid4()
    event = ChatEvent(
        conversation_id=uuid4(),
        cursor=1,
        turn_id=uuid4(),
        created_at=datetime.now(UTC),
        payload=ChatRunActivity(
            activity=RunActivity(
                run_id=run_id,
                sequence=1,
                tool_name=RunActivityTool.READ_FILE,
                state="started",
                created_at=datetime.now(UTC),
            )
        ),
    )
    assert ChatEvent.model_validate_json(event.model_dump_json()).payload.type == "run.activity"
    status = event.model_copy(
        update={"payload": ChatRunActivityStatus(run_id=run_id, state="truncated", last_sequence=1)}
    )
    assert (
        ChatEvent.model_validate_json(status.model_dump_json()).payload.type
        == "run.activity_status"
    )
    assert isinstance(event.payload, ChatRunActivity)
    with pytest.raises(ValidationError):
        ChatEvent.model_validate(
            {
                **event.model_dump(mode="json"),
                "payload": {
                    **event.payload.model_dump(mode="json"),
                    "activity": {
                        **event.payload.activity.model_dump(mode="json"),
                        "arguments": {"path": "/private"},
                    },
                },
            }
        )


def test_chat_coding_result_matches_tool() -> None:
    from coire_core.models.mcp import McpToolName, ResearchResult

    result = ResearchResult.model_validate(
        {
            "result_id": str(uuid4()),
            "run_id": str(uuid4()),
            "source_revision": "a" * 40,
            "answer": "Found code",
            "citations": [{"path": "main.py", "line": 1}],
        }
    )
    payload = ChatTurnResult(tool=McpToolName.RESEARCH, result=result)
    assert payload.type == "turn.result"
    with pytest.raises(ValidationError):
        ChatTurnResult(tool=McpToolName.APPLY, result=result)


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
