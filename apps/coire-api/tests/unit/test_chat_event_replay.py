"""Observer cursor gaps replace old deltas with a current private snapshot."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coire_api.auth import Principal, PrincipalKind
from coire_api.chat import streaming
from coire_api.db import ChatConversationRow, ChatEventRow, UserRow
from coire_core.models.chat import (
    ChatConversation,
    ChatConversationDeleted,
    ChatConversationDetail,
    ChatTurnStatus,
)
from coire_core.settings import Settings


async def test_expired_observer_cursor_gets_replacement_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.chat import service

    now = datetime.now(UTC)
    owner = uuid.uuid4()
    conversation = ChatConversationRow(
        id=uuid.uuid4(),
        owner_user_id=owner,
        title="Private",
        mode="chat",
        revision=2,
        event_cursor=5,
        created_at=now,
        updated_at=now,
    )
    retained = ChatEventRow(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        cursor=4,
        type="turn.status",
        payload=ChatTurnStatus(state="running").model_dump(mode="json"),
        created_at=now,
        expires_at=now,
    )

    class Session:
        async def get(self, model: type, _id: uuid.UUID) -> object:
            if model is UserRow:
                return SimpleNamespace(active=True)
            if model is ChatConversationRow:
                return conversation
            return None

        async def execute(self, _statement: object) -> SimpleNamespace:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [retained]))

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    detail = ChatConversationDetail(
        conversation=ChatConversation(
            id=conversation.id,
            owner_id=owner,
            title="Private",
            mode="chat",
            revision=2,
            created_at=now,
            updated_at=now,
        ),
        event_cursor=5,
    )
    get_detail = AsyncMock(return_value=detail)
    monkeypatch.setattr(streaming, "session_scope", sessions)
    monkeypatch.setattr(service, "get_conversation_detail", get_detail)
    principal = Principal(kind=PrincipalKind.USER, user_id=owner)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
    chunks = [
        chunk
        async for chunk in streaming.observe_conversation(
            conversation.id,
            principal,
            request,  # type: ignore[arg-type]
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
            1,
        )
    ]
    assert len(chunks) == 1
    assert b"event: snapshot" in chunks[0]
    assert f"id: {conversation.id}:5".encode() in chunks[0]
    assert b'"replacement":true' in chunks[0]
    get_detail.assert_awaited_once()


async def test_existing_observer_receives_deletion_event_then_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    owner = uuid.uuid4()
    conversation = ChatConversationRow(
        id=uuid.uuid4(),
        owner_user_id=owner,
        title="Private",
        mode="chat",
        revision=3,
        event_cursor=4,
        deleted_at=now,
        created_at=now,
        updated_at=now,
    )
    deleted = ChatEventRow(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        cursor=4,
        type="conversation.deleted",
        payload=ChatConversationDeleted(conversation_id=conversation.id, revision=3).model_dump(
            mode="json"
        ),
        created_at=now,
        expires_at=now,
    )

    class Session:
        async def get(self, model: type, _id: uuid.UUID) -> object:
            if model is UserRow:
                return SimpleNamespace(active=True)
            if model is ChatConversationRow:
                return conversation
            return None

        async def scalar(self, _statement: object) -> ChatEventRow:
            return deleted

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(streaming, "session_scope", sessions)
    chunks = [
        chunk
        async for chunk in streaming.observe_conversation(
            conversation.id,
            Principal(kind=PrincipalKind.USER, user_id=owner),
            SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),  # type: ignore[arg-type]
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
            3,
        )
    ]
    assert len(chunks) == 1
    assert b"event: conversation.deleted" in chunks[0]
    assert b"Private" not in chunks[0]
