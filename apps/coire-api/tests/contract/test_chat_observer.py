"""The conversation observer is owner scoped and has no generation authority."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.chat.streaming import encode_event
from coire_api.db import ChatConversationRow, get_session
from coire_core.models.chat import ChatEvent, ChatTurnStatus
from coire_core.settings import Settings


async def test_observer_checks_owner_and_scopes_replay_cursor(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from coire_api.routes import chat

    owner = uuid.uuid4()
    conversation = ChatConversationRow(
        id=uuid.uuid4(),
        owner_user_id=owner,
        title="Private",
        mode="chat",
        revision=1,
        event_cursor=4,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class Session:
        async def get(self, _model: object, _id: uuid.UUID) -> ChatConversationRow:
            return conversation

    async def fake_session() -> AsyncIterator[Session]:
        yield Session()

    seen: list[int | None] = []

    async def observe(
        _id: uuid.UUID,
        _principal: Principal,
        _request: object,
        _settings: Settings,
        cursor: int | None,
    ) -> AsyncIterator[bytes]:
        seen.append(cursor)
        event = ChatEvent(
            conversation_id=conversation.id,
            cursor=4,
            created_at=datetime.now(UTC),
            payload=ChatTurnStatus(state="running"),
        )
        yield encode_event(event)

    monkeypatch.setattr(chat, "observe_conversation", observe)
    app = create_app(
        Settings(  # type: ignore[call-arg]
            _secrets_dir="/nonexistent",
            chat_enabled=True,
            identity_legacy_admin_enabled=True,
            admin_token=SecretStr("observer-test"),
        )
    )
    active = [Principal(kind=PrincipalKind.USER, user_id=owner)]
    app.dependency_overrides[require_principal] = lambda: active[0]
    app.dependency_overrides[get_session] = fake_session
    path = f"/api/v1/chat/conversations/{conversation.id}/events"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        good = await client.get(
            path,
            headers={
                "Authorization": "Bearer observer-test",
                "Last-Event-ID": f"{conversation.id}:3",
            },
        )
        wrong_cursor = await client.get(
            path,
            headers={
                "Authorization": "Bearer observer-test",
                "Last-Event-ID": f"{uuid.uuid4()}:3",
            },
        )
        active[0] = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
        foreign = await client.get(path, headers={"Authorization": "Bearer observer-test"})
    assert good.status_code == 200
    assert good.headers["content-type"].startswith("text/event-stream")
    assert f"id: {conversation.id}:4" in good.text
    assert seen == [3]
    assert wrong_cursor.status_code == 409
    assert foreign.status_code == 404
