"""Owner Stop is durable, idempotent and scoped to the conversation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.chat.turns import request_turn_stop
from coire_api.db import ChatConversationRow, ChatEventRow, ChatTurnRow, get_session
from coire_core.errors import ChatNotFound
from coire_core.models.chat import ChatStopRequest
from coire_core.settings import Settings

NOW = datetime.now(UTC)


class StopSession:
    def __init__(self, *, allowed: bool = True) -> None:
        self.owner = uuid.uuid4()
        self.conversation = ChatConversationRow(
            id=uuid.uuid4(),
            owner_user_id=self.owner,
            title="Saved",
            mode="chat",
            revision=2,
            event_cursor=3,
            created_at=NOW,
            updated_at=NOW,
        )
        self.turn = ChatTurnRow(
            id=uuid.uuid4(),
            conversation_id=self.conversation.id,
            client_request_id=uuid.uuid4(),
            request_hash="a" * 64,
            accepted_revision=1,
            input_message_id=uuid.uuid4(),
            assistant_message_id=uuid.uuid4(),
            model_id=uuid.uuid4(),
            model_display_name="Saved model",
            action="chat",
            state="running",
            created_at=NOW,
            updated_at=NOW,
        )
        self.allowed = allowed
        self.events: list[ChatEventRow] = []
        self.commits = 0

    async def scalar(self, _statement: object) -> ChatConversationRow | None:
        return self.conversation if self.allowed else None

    async def get(self, _model: object, identifier: uuid.UUID) -> ChatTurnRow | None:
        return self.turn if identifier == self.turn.id else None

    def add(self, event: ChatEventRow) -> None:
        self.events.append(event)

    async def commit(self) -> None:
        self.commits += 1


async def test_owner_stop_persists_one_status_and_repeats_idempotently() -> None:
    session = StopSession()
    principal = Principal(kind=PrincipalKind.USER, user_id=session.owner)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    first = await request_turn_stop(
        session,
        principal,
        session.conversation.id,
        session.turn.id,
        ChatStopRequest(reason="user_stop"),
        settings,
    )  # type: ignore[arg-type]
    second = await request_turn_stop(
        session,
        principal,
        session.conversation.id,
        session.turn.id,
        ChatStopRequest(reason="user_stop"),
        settings,
    )  # type: ignore[arg-type]
    assert first.state == second.state == "stop_requested"
    assert session.turn.stop_reason == "user_stop"
    assert session.conversation.event_cursor == 4
    assert len(session.events) == session.commits == 1
    assert session.events[0].payload == {
        "type": "turn.status",
        "state": "stop_requested",
        "estimate_seconds": None,
        "queue_position": None,
        "explanation": None,
    }
    session.turn.state = "stopped"
    finished = await request_turn_stop(
        session,
        principal,
        session.conversation.id,
        session.turn.id,
        ChatStopRequest(reason="user_stop"),
        settings,
    )  # type: ignore[arg-type]
    assert finished.state == "stopped"
    assert session.commits == 1


async def test_foreign_missing_and_cross_parent_stop_are_uniform_404() -> None:
    session = StopSession(allowed=False)
    principal = Principal(kind=PrincipalKind.ADMIN, user_id=uuid.uuid4())
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    with pytest.raises(ChatNotFound):
        await request_turn_stop(
            session,
            principal,
            session.conversation.id,
            session.turn.id,
            ChatStopRequest(reason="navigation"),
            settings,
        )  # type: ignore[arg-type]
    session.allowed = True
    with pytest.raises(ChatNotFound):
        await request_turn_stop(
            session,
            principal,
            session.conversation.id,
            uuid.uuid4(),
            ChatStopRequest(reason="navigation"),
            settings,
        )  # type: ignore[arg-type]
    session.turn.conversation_id = uuid.uuid4()
    with pytest.raises(ChatNotFound):
        await request_turn_stop(
            session,
            principal,
            session.conversation.id,
            session.turn.id,
            ChatStopRequest(reason="navigation"),
            settings,
        )  # type: ignore[arg-type]


async def test_stop_route_requires_exact_browser_origin_and_returns_typed_state() -> None:
    session = StopSession()
    settings = Settings(
        _secrets_dir="/nonexistent",
        chat_enabled=True,
        chat_browser_origin="http://localhost",
        identity_legacy_admin_enabled=True,
        admin_token=SecretStr("stop-test"),
    )  # type: ignore[call-arg]
    app = create_app(settings)
    app.dependency_overrides[require_principal] = lambda: Principal(
        kind=PrincipalKind.USER, user_id=session.owner
    )

    async def fake_session():  # type: ignore[no-untyped-def]
        yield session

    app.dependency_overrides[get_session] = fake_session
    path = f"/api/v1/chat/conversations/{session.conversation.id}/turns/{session.turn.id}/stop"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        denied = await client.post(
            path, json={"reason": "user_stop"}, headers={"Authorization": "Bearer stop-test"}
        )
        accepted = await client.post(
            path,
            json={"reason": "user_stop"},
            headers={"Authorization": "Bearer stop-test", "Origin": "http://localhost"},
        )
    assert denied.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["state"] == "stop_requested"
    assert accepted.json()["model_display_name"] == "Saved model"
