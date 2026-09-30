"""Versioned owner edits preserve conversation history and notify observers."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.chat.service import update_conversation
from coire_api.db import ChatConversationRow, ChatEventRow, get_session
from coire_core.errors import ChatConflict, ChatNotFound
from coire_core.models.chat import ChatConversationUpdate
from coire_core.settings import Settings


class EditSession:
    def __init__(self, *, allowed: bool = True) -> None:
        self.owner = uuid.uuid4()
        now = datetime.now(UTC)
        self.row = ChatConversationRow(
            id=uuid.uuid4(),
            owner_user_id=self.owner,
            title="Before",
            mode="chat",
            revision=2,
            event_cursor=3,
            created_at=now,
            updated_at=now,
        )
        self.allowed = allowed
        self.events: list[ChatEventRow] = []
        self.commits = 0
        self.sql = ""

    async def scalar(self, statement: object) -> ChatConversationRow | None:
        self.sql = str(statement)
        return self.row if self.allowed else None

    def add(self, row: ChatEventRow) -> None:
        self.events.append(row)

    async def commit(self) -> None:
        self.commits += 1


async def test_owner_edit_commits_one_version_and_event() -> None:
    session = EditSession()
    principal = Principal(kind=PrincipalKind.USER, user_id=session.owner)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    changed = await update_conversation(
        session,  # type: ignore[arg-type]
        principal,
        session.row.id,
        ChatConversationUpdate(expected_revision=2, title="After"),
        settings,
    )
    assert changed.title == "After" and changed.revision == 3
    assert session.row.event_cursor == 4
    assert session.events[0].type == "conversation.updated"
    assert session.events[0].payload["conversation"]["title"] == "After"  # type: ignore[index]
    assert session.commits == 1
    assert "FOR UPDATE" in session.sql and "owner_user_id" in session.sql
    with pytest.raises(ChatConflict):
        await update_conversation(
            session,  # type: ignore[arg-type]
            principal,
            session.row.id,
            ChatConversationUpdate(expected_revision=2, title="Stale"),
            settings,
        )
    assert len(session.events) == 1


async def test_foreign_edit_and_active_mode_change_are_refused() -> None:
    session = EditSession(allowed=False)
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    with pytest.raises(ChatNotFound):
        await update_conversation(
            session,  # type: ignore[arg-type]
            principal,
            session.row.id,
            ChatConversationUpdate(expected_revision=2, title="Foreign"),
            settings,
        )
    session.allowed = True
    session.row.active_turn_id = uuid.uuid4()
    with pytest.raises(ChatConflict):
        await update_conversation(
            session,  # type: ignore[arg-type]
            Principal(kind=PrincipalKind.USER, user_id=session.owner),
            session.row.id,
            ChatConversationUpdate(expected_revision=2, mode="code"),
            settings,
        )


async def test_edit_route_checks_browser_origin_and_returns_revision() -> None:
    session = EditSession()
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        chat_enabled=True,
        chat_browser_origin="http://localhost",
        identity_legacy_admin_enabled=True,
        admin_token=SecretStr("edit-test"),
    )
    app = create_app(settings)
    app.dependency_overrides[require_principal] = lambda: Principal(
        kind=PrincipalKind.USER, user_id=session.owner
    )

    async def fake_session() -> AsyncIterator[EditSession]:
        yield session

    app.dependency_overrides[get_session] = fake_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        path = f"/api/v1/chat/conversations/{session.row.id}"
        refused = await client.patch(
            path,
            json={"expected_revision": 2, "title": "After"},
            headers={"Authorization": "Bearer edit-test"},
        )
        accepted = await client.patch(
            path,
            json={"expected_revision": 2, "title": "After"},
            headers={"Authorization": "Bearer edit-test", "Origin": "http://localhost"},
        )
    assert refused.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["revision"] == 3
