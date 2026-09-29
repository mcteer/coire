"""Locked native text-turn admission contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.chat.turns import admit_turn, project_message, project_turn, read_turn_detail
from coire_api.db import (
    ChatConversationRow,
    ChatEventRow,
    ChatMessageRow,
    ChatTurnRow,
    ModelRow,
    get_session,
)
from coire_core.errors import ChatConflict, ChatContextExceeded, ChatNotFound
from coire_core.models.chat import ChatTurnCreate, ChatTurnDetail
from coire_core.models.registry import ModelState, Visibility
from coire_core.settings import Settings

NOW = datetime.now(UTC)


def _model(
    *, context_window: int = 4096, visibility: Visibility = Visibility.PUBLISHED
) -> ModelRow:
    return ModelRow(
        id=uuid.uuid4(),
        repo_id="owner/model",
        slug="owner--model",
        display_name="Named model",
        state=ModelState.READY,
        visibility=visibility,
        entitlement=[],
        tags=["general"],
        precision="4bit",
        weight_bytes=1,
        memory_estimate_bytes=2,
        context_window=context_window,
        capability_profile={},
        backend="mlx_lm",
    )


class Session:
    def __init__(self, conversation: ChatConversationRow, model: ModelRow) -> None:
        self.conversation = conversation
        self.model = model
        self.messages: list[ChatMessageRow] = []
        self.turns: list[ChatTurnRow] = []
        self.events: list[ChatEventRow] = []
        self.sql: list[str] = []
        self.commits = 0

    async def scalar(self, statement: object) -> object:
        sql = str(statement)
        self.sql.append(sql)
        if "FROM chat_conversations" in sql:
            return self.conversation
        if "FROM chat_turns" in sql:
            return self.turns[0] if self.turns else None
        return None

    async def execute(self, statement: object) -> object:
        self.sql.append(str(statement))
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.messages)))

    async def get(self, model: object, identifier: uuid.UUID) -> ModelRow | None:
        return self.model if self.model.id == identifier else None

    def add(self, row: object) -> None:
        if isinstance(row, ChatMessageRow):
            self.messages.append(row)
        elif isinstance(row, ChatTurnRow):
            self.turns.append(row)
        elif isinstance(row, ChatEventRow):
            self.events.append(row)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


def _case() -> tuple[Session, Principal, ChatTurnCreate]:
    owner_id = uuid.uuid4()
    conversation = ChatConversationRow(
        id=uuid.uuid4(),
        owner_user_id=owner_id,
        title="Draft",
        mode="chat",
        revision=1,
        event_cursor=0,
        created_at=NOW,
        updated_at=NOW,
    )
    model = _model()
    session = Session(conversation, model)
    principal = Principal(kind=PrincipalKind.USER, user_id=owner_id)
    body = ChatTurnCreate(
        client_request_id=uuid.uuid4(), expected_revision=1, model_id=model.id, content="Hello"
    )
    return session, principal, body


async def test_send_persists_input_assistant_turn_and_event_before_stream() -> None:
    session, principal, body = _case()
    session.messages.append(
        ChatMessageRow(
            id=uuid.uuid4(),
            conversation_id=session.conversation.id,
            position=1,
            role="user",
            text="Earlier",
            reasoning="",
            attachment_ids=[],
            created_at=NOW,
        )
    )
    admission = await admit_turn(
        session, session.conversation.id, principal, body, Settings(_secrets_dir="/nonexistent")
    )  # type: ignore[arg-type,call-arg]
    assert not admission.replay
    assert session.commits == 1
    assert any("FOR UPDATE" in sql for sql in session.sql)
    assert [message.content for message in admission.history] == ["Earlier", "Hello"]
    assert [message.position for message in session.messages] == [1, 2, 3]
    assert session.messages[-1].role == "assistant" and session.messages[-1].text == ""
    assert session.turns[0].model_display_name == "Named model"
    assert session.messages[-1].model_id == body.model_id
    assert session.events[0].type == "turn.accepted"
    assert session.conversation.event_cursor == 1
    assert session.conversation.revision == 2
    assert session.conversation.active_turn_id == session.turns[0].id


async def test_replay_is_idempotent_but_changed_body_conflicts() -> None:
    session, principal, body = _case()
    first = await admit_turn(
        session, session.conversation.id, principal, body, Settings(_secrets_dir="/nonexistent")
    )  # type: ignore[arg-type,call-arg]
    second = await admit_turn(
        session, session.conversation.id, principal, body, Settings(_secrets_dir="/nonexistent")
    )  # type: ignore[arg-type,call-arg]
    assert first.turn.id == second.turn.id and second.replay
    assert len(session.turns) == 1 and session.commits == 1
    with pytest.raises(ChatConflict):
        await admit_turn(
            session,
            session.conversation.id,
            principal,
            body.model_copy(update={"content": "Changed"}),
            Settings(_secrets_dir="/nonexistent"),
        )  # type: ignore[arg-type,call-arg]


async def test_owner_revision_active_and_model_rules_precede_writes() -> None:
    session, principal, body = _case()
    with pytest.raises(ChatNotFound):
        await admit_turn(
            session,
            session.conversation.id,
            principal.model_copy(update={"user_id": uuid.uuid4()}),
            body,
            Settings(_secrets_dir="/nonexistent"),
        )  # type: ignore[arg-type,call-arg]
    with pytest.raises(ChatConflict):
        await admit_turn(
            session,
            session.conversation.id,
            principal,
            body.model_copy(update={"expected_revision": 2}),
            Settings(_secrets_dir="/nonexistent"),
        )  # type: ignore[arg-type,call-arg]
    session.conversation.active_turn_id = uuid.uuid4()
    with pytest.raises(ChatConflict):
        await admit_turn(
            session, session.conversation.id, principal, body, Settings(_secrets_dir="/nonexistent")
        )  # type: ignore[arg-type,call-arg]
    session.conversation.active_turn_id = None
    session.model.visibility = Visibility.ADMIN_ONLY
    with pytest.raises(ChatNotFound):
        await admit_turn(
            session, session.conversation.id, principal, body, Settings(_secrets_dir="/nonexistent")
        )  # type: ignore[arg-type,call-arg]
    session.model.visibility = Visibility.PUBLISHED
    with pytest.raises(ChatConflict):
        await admit_turn(
            session,
            session.conversation.id,
            principal,
            body.model_copy(update={"action": "apply"}),
            Settings(_secrets_dir="/nonexistent"),
        )  # type: ignore[arg-type,call-arg]
    assert session.commits == 0


async def test_full_history_context_preflight_refuses_without_dropping() -> None:
    session, principal, body = _case()
    session.model.context_window = 20
    session.messages.append(
        ChatMessageRow(
            id=uuid.uuid4(),
            conversation_id=session.conversation.id,
            position=1,
            role="user",
            text="Earlier " * 20,
            reasoning="",
            attachment_ids=[],
            created_at=NOW,
        )
    )
    with pytest.raises(ChatContextExceeded):
        await admit_turn(
            session, session.conversation.id, principal, body, Settings(_secrets_dir="/nonexistent")
        )  # type: ignore[arg-type,call-arg]
    assert len(session.messages) == 1 and not session.turns and session.commits == 0


async def test_small_context_gets_bounded_output_allowance() -> None:
    session, principal, body = _case()
    session.model.context_window = 128
    admission = await admit_turn(
        session,
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )  # type: ignore[arg-type]
    assert admission.output_tokens == 32


async def test_model_switch_preserves_earlier_model_snapshot_in_history() -> None:
    session, principal, body = _case()
    previous_id = session.model.id
    session.messages.extend(
        [
            ChatMessageRow(
                id=uuid.uuid4(),
                conversation_id=session.conversation.id,
                position=1,
                role="user",
                text="First question",
                reasoning="",
                model_id=previous_id,
                model_display_name="Previous model",
                attachment_ids=[],
                created_at=NOW,
            ),
            ChatMessageRow(
                id=uuid.uuid4(),
                conversation_id=session.conversation.id,
                position=2,
                role="assistant",
                text="First answer",
                reasoning="",
                model_id=previous_id,
                model_display_name="Previous model",
                attachment_ids=[],
                created_at=NOW,
            ),
        ]
    )
    session.model = _model()
    session.conversation.revision = 2
    switched = body.model_copy(update={"expected_revision": 2, "model_id": session.model.id})
    admission = await admit_turn(
        session,
        session.conversation.id,
        principal,
        switched,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )  # type: ignore[arg-type]
    assert [message.content for message in admission.history] == [
        "First question",
        "First answer",
        "Hello",
    ]
    assert session.messages[1].model_id == previous_id
    assert session.messages[-1].model_id == session.model.id
    assert admission.turn.model_display_name == session.model.display_name


async def test_turn_status_refuses_cross_conversation_id() -> None:
    session, principal, body = _case()
    admission = await admit_turn(
        session,
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )  # type: ignore[arg-type]
    admission.turn.conversation_id = uuid.uuid4()

    class StatusSession:
        async def get(self, model: object, _identifier: object) -> object:
            return session.conversation if model is ChatConversationRow else admission.turn

    with pytest.raises(ChatNotFound):
        await read_turn_detail(
            StatusSession(),
            principal,
            session.conversation.id,
            admission.turn.id,  # type: ignore[arg-type]
        )


async def test_send_and_status_routes_use_native_sse_and_owner_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.routes import chat as chat_routes

    session, principal, body = _case()
    admission = await admit_turn(
        session, session.conversation.id, principal, body, Settings(_secrets_dir="/nonexistent")
    )  # type: ignore[arg-type,call-arg]
    settings = Settings(
        _secrets_dir="/nonexistent",
        chat_enabled=True,
        chat_browser_origin="http://localhost",
        identity_legacy_admin_enabled=True,
        admin_token=SecretStr("chat-turn-contract"),
    )  # type: ignore[call-arg]
    app = create_app(settings)
    app.dependency_overrides[require_principal] = lambda: principal

    async def fake_session():  # type: ignore[no-untyped-def]
        yield session

    async def fake_admit(*_args: object) -> object:
        return admission

    async def fake_stream(*_args: object):  # type: ignore[no-untyped-def]
        yield b"event: turn.accepted\ndata: {}\n\n"

    async def fake_status(*_args: object) -> ChatTurnDetail:
        return ChatTurnDetail(
            turn=project_turn(session.turns[0]),
            input_message=project_message(session.messages[0]),
            assistant_message=project_message(session.messages[1]),
            event_cursor=1,
        )

    app.dependency_overrides[get_session] = fake_session
    monkeypatch.setattr(chat_routes, "admit_turn", fake_admit)
    monkeypatch.setattr(chat_routes, "native_stream", fake_stream)
    monkeypatch.setattr(chat_routes, "read_turn_detail", fake_status)
    path = f"/api/v1/chat/conversations/{session.conversation.id}/turns"
    schema = app.openapi()
    stream_schema = schema["paths"]["/api/v1/chat/conversations/{conversation_id}/turns"]["post"][
        "responses"
    ]["200"]["content"]["text/event-stream"]["schema"]
    assert stream_schema["$ref"] == "#/components/schemas/ChatEvent"
    headers = {"Authorization": "Bearer chat-turn-contract"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        denied = await client.post(path, json=body.model_dump(mode="json"), headers=headers)
        assert denied.status_code == 403
        accepted = await client.post(
            path,
            json=body.model_dump(mode="json"),
            headers={**headers, "Origin": "http://localhost"},
        )
        assert accepted.status_code == 200
        assert accepted.headers["content-type"].startswith("text/event-stream")
        assert accepted.text.startswith("event: turn.accepted")
        status = await client.get(f"{path}/{admission.turn.id}", headers=headers)
        assert status.status_code == 200
        assert status.json()["turn"]["model_display_name"] == "Named model"
