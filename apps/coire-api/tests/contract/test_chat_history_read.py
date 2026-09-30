"""Owner-scoped Chat history list and detail contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.dialects import postgresql

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.chat.service import get_conversation_detail, list_conversations
from coire_api.db import ChatConversationRow, ChatMessageRow, ChatTurnRow, get_session
from coire_core.errors import ChatConflict, ChatNotFound
from coire_core.models.chat import ChatMessagePageQuery, ChatPageQuery
from coire_core.settings import Settings

NOW = datetime.now(UTC)
OWNER = uuid.uuid4()


def _conversation(*, owner: uuid.UUID = OWNER, older: int = 0) -> ChatConversationRow:
    return ChatConversationRow(
        id=uuid.uuid4(),
        owner_user_id=owner,
        title="Private notes",
        mode="chat",
        revision=2,
        event_cursor=4,
        active_turn_id=None,
        created_at=NOW - timedelta(minutes=older),
        updated_at=NOW - timedelta(minutes=older),
    )


class FakeHistorySession:
    def __init__(
        self,
        conversations: list[ChatConversationRow] | None = None,
        detail: ChatConversationRow | None = None,
        messages: list[ChatMessageRow] | None = None,
        turns: list[ChatTurnRow] | None = None,
    ) -> None:
        self.conversations = conversations or []
        self.detail = detail
        self.messages = messages or []
        self.turns = turns or []
        self.queries: list[str] = []

    async def scalar(self, statement: object) -> ChatConversationRow | None:
        self.queries.append(str(statement.compile(dialect=postgresql.dialect())))  # type: ignore[attr-defined, no-untyped-call]
        return self.detail

    async def execute(self, statement: object) -> object:
        sql = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined, no-untyped-call]
        self.queries.append(sql)
        rows: list[object]
        if "FROM chat_turns" in sql:
            rows = self.turns  # type: ignore[assignment]
        elif "FROM chat_messages" in sql:
            rows = self.messages  # type: ignore[assignment]
        elif "FROM chat_attachments" in sql:
            rows = []
        else:
            rows = self.conversations  # type: ignore[assignment]
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


async def test_list_uses_owner_filter_and_stable_opaque_cursor() -> None:
    principal = Principal(kind=PrincipalKind.ADMIN, user_id=OWNER)
    rows = [_conversation(older=offset) for offset in range(3)]
    session = FakeHistorySession(conversations=rows)
    first = await list_conversations(session, principal, ChatPageQuery(limit=2))  # type: ignore[arg-type]
    assert [item.id for item in first.data] == [rows[0].id, rows[1].id]
    assert first.next_cursor and rows[1].id.hex not in first.next_cursor
    assert "owner_user_id" in session.queries[0]
    assert "deleted_at IS NULL" in session.queries[0]
    assert "updated_at DESC" in session.queries[0]
    later = FakeHistorySession(conversations=[rows[2]])
    second = await list_conversations(
        later,  # type: ignore[arg-type]
        principal,
        ChatPageQuery(limit=2, cursor=first.next_cursor),
    )
    assert [item.id for item in second.data] == [rows[2].id]
    assert second.next_cursor is None
    assert "updated_at <" in later.queries[0]
    for value in ("bad!", "e30", "bnVsbA"):
        with pytest.raises(ChatConflict):
            await list_conversations(session, principal, ChatPageQuery(cursor=value))  # type: ignore[arg-type]


async def test_detail_returns_partial_answer_snapshot_and_older_position() -> None:
    conversation = _conversation()
    user = ChatMessageRow(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        position=3,
        role="user",
        text="Question",
        reasoning="",
        attachment_ids=[],
        created_at=NOW,
    )
    answer = ChatMessageRow(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        position=4,
        role="assistant",
        text="Partial answer",
        reasoning="",
        model_display_name="Saved model name",
        attachment_ids=[],
        created_at=NOW,
    )
    turn = ChatTurnRow(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        client_request_id=uuid.uuid4(),
        request_hash="a" * 64,
        accepted_revision=1,
        input_message_id=user.id,
        assistant_message_id=answer.id,
        model_id=uuid.uuid4(),
        model_display_name="Saved model name",
        action="chat",
        state="running",
        created_at=NOW,
        updated_at=NOW,
    )
    session = FakeHistorySession(detail=conversation, messages=[answer, user], turns=[turn])
    detail = await get_conversation_detail(
        session,  # type: ignore[arg-type]
        Principal(kind=PrincipalKind.USER, user_id=OWNER),
        conversation.id,
        ChatMessagePageQuery(limit=1),
    )
    assert detail.event_cursor == 4
    assert detail.messages[0].text == "Partial answer"
    assert detail.messages[0].model_display_name == "Saved model name"
    assert detail.turns[0].state == "running"
    assert detail.next_message_position == 4
    assert "FOR SHARE" in session.queries[0]
    assert "owner_user_id" in session.queries[0]


async def test_missing_foreign_and_deleted_detail_share_safe_404() -> None:
    settings = Settings(
        _secrets_dir="/nonexistent",
        chat_enabled=True,
        identity_legacy_admin_enabled=True,
        admin_token=SecretStr("history-test"),
    )  # type: ignore[call-arg]
    principal = Principal(kind=PrincipalKind.ADMIN, user_id=OWNER)
    app = create_app(settings)
    app.dependency_overrides[require_principal] = lambda: principal
    session = FakeHistorySession(detail=None)

    async def fake_session():  # type: ignore[no-untyped-def]
        yield session

    app.dependency_overrides[get_session] = fake_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        responses = [
            await client.get(
                f"/api/v1/chat/conversations/{uuid.uuid4()}",
                headers={"Authorization": "Bearer history-test"},
            )
            for _ in range(3)
        ]
    assert [response.status_code for response in responses] == [404, 404, 404]
    assert (
        len(
            {
                (response.json()["title"], response.json()["detail"], response.json()["coire_code"])
                for response in responses
            }
        )
        == 1
    )
    with pytest.raises(ChatNotFound):
        await get_conversation_detail(session, principal, uuid.uuid4(), ChatMessagePageQuery())  # type: ignore[arg-type]


async def test_history_routes_return_typed_owner_pages() -> None:
    conversation = _conversation()
    settings = Settings(
        _secrets_dir="/nonexistent",
        chat_enabled=True,
        identity_legacy_admin_enabled=True,
        admin_token=SecretStr("history-test"),
    )  # type: ignore[call-arg]
    app = create_app(settings)
    app.dependency_overrides[require_principal] = lambda: Principal(
        kind=PrincipalKind.USER, user_id=OWNER
    )
    session = FakeHistorySession(conversations=[conversation], detail=conversation)

    async def fake_session():  # type: ignore[no-untyped-def]
        yield session

    app.dependency_overrides[get_session] = fake_session
    headers = {"Authorization": "Bearer history-test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        listed = await client.get("/api/v1/chat/conversations", headers=headers)
        detail = await client.get(f"/api/v1/chat/conversations/{conversation.id}", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["data"][0]["id"] == str(conversation.id)
    assert detail.status_code == 200
    assert detail.json()["conversation"]["owner_id"] == str(OWNER)
    assert detail.json()["event_cursor"] == 4
