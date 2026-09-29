"""Chat deletion hides content immediately and requests active generation Stop."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind, require_owned_chat, require_principal
from coire_api.chat.service import delete_conversation
from coire_api.db import ChatConversationRow, ChatEventRow, ChatTurnRow, McpArtifactRow, get_session
from coire_core.errors import ChatConflict, ChatNotFound
from coire_core.models.chat import ChatDeleteRequest
from coire_core.settings import Settings


class DeleteSession:
    def __init__(self, *, allowed: bool = True) -> None:
        now = datetime.now(UTC)
        self.owner = uuid.uuid4()
        self.conversation = ChatConversationRow(
            id=uuid.uuid4(),
            owner_user_id=self.owner,
            title="Private",
            mode="chat",
            revision=2,
            event_cursor=3,
            created_at=now,
            updated_at=now,
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
            model_display_name="Model",
            action="chat",
            state="running",
            created_at=now,
            updated_at=now,
        )
        self.conversation.active_turn_id = self.turn.id
        self.allowed = allowed
        self.events: list[ChatEventRow] = []
        self.artifacts: list[McpArtifactRow] = []
        self.commits = 0

    async def scalar(self, _statement: object) -> ChatConversationRow | None:
        return self.conversation if self.allowed else None

    async def get(self, _model: object, identifier: uuid.UUID) -> ChatTurnRow | None:
        return self.turn if identifier == self.turn.id else None

    async def execute(self, statement: object) -> object:
        assert "chat_turns.coding_call_id" in str(statement)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.artifacts))

    def add(self, row: ChatEventRow) -> None:
        self.events.append(row)

    async def commit(self) -> None:
        self.commits += 1


async def test_owner_tombstone_is_immediate_and_idempotent() -> None:
    session = DeleteSession()
    principal = Principal(kind=PrincipalKind.USER, user_id=session.owner)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    first = await delete_conversation(
        session,
        principal,
        session.conversation.id,
        ChatDeleteRequest(expected_revision=2),
        settings,
    )  # type: ignore[arg-type]
    second = await delete_conversation(
        session,
        principal,
        session.conversation.id,
        ChatDeleteRequest(expected_revision=2),
        settings,
    )  # type: ignore[arg-type]
    assert first == second
    assert session.conversation.deleted_at is not None
    assert session.conversation.revision == 3
    assert session.turn.state == "stop_requested"
    assert session.turn.stop_reason == "conversation_deleted"
    assert session.events[0].type == "conversation.deleted"
    assert "Private" not in str(session.events[0].payload)
    assert len(session.events) == session.commits == 1

    class ReadSession:
        async def get(self, _model: object, _id: uuid.UUID) -> ChatConversationRow:
            return session.conversation

    with pytest.raises(ChatNotFound):
        await require_owned_chat(ReadSession(), session.conversation.id, principal)  # type: ignore[arg-type]


async def test_foreign_and_stale_delete_are_refused() -> None:
    session = DeleteSession(allowed=False)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    with pytest.raises(ChatNotFound):
        await delete_conversation(
            session,
            Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4()),
            session.conversation.id,
            ChatDeleteRequest(expected_revision=2),
            settings,
        )  # type: ignore[arg-type]
    session.allowed = True
    with pytest.raises(ChatConflict):
        await delete_conversation(
            session,
            Principal(kind=PrincipalKind.USER, user_id=session.owner),
            session.conversation.id,
            ChatDeleteRequest(expected_revision=1),
            settings,
        )  # type: ignore[arg-type]


async def test_delete_expires_completed_chat_coding_artifact_before_commit() -> None:
    session = DeleteSession()
    session.conversation.active_turn_id = None
    session.turn.action = "research"
    session.turn.state = "completed"
    call_id = uuid.uuid4()
    session.turn.coding_call_id = call_id
    artifact = McpArtifactRow(
        id=uuid.uuid4(),
        owner_user_id=session.owner,
        run_id=uuid.uuid4(),
        call_id=call_id,
        storage_ref="edge-a",
        sha256="a" * 64,
        size_bytes=512,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session.artifacts.append(artifact)
    await delete_conversation(
        session,
        Principal(kind=PrincipalKind.USER, user_id=session.owner),
        session.conversation.id,
        ChatDeleteRequest(expected_revision=2),
        Settings(_secrets_dir="/nonexistent"),
    )  # type: ignore[arg-type,call-arg]
    assert artifact.expires_at <= session.conversation.deleted_at
    assert session.commits == 1


async def test_delete_route_requires_browser_origin() -> None:
    session = DeleteSession()
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        chat_enabled=True,
        chat_browser_origin="http://localhost",
        identity_legacy_admin_enabled=True,
        admin_token=SecretStr("delete-test"),
    )
    app = create_app(settings)
    app.dependency_overrides[require_principal] = lambda: Principal(
        kind=PrincipalKind.USER, user_id=session.owner
    )

    async def fake_session() -> AsyncIterator[DeleteSession]:
        yield session

    app.dependency_overrides[get_session] = fake_session
    path = f"/api/v1/chat/conversations/{session.conversation.id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        refused = await client.request(
            "DELETE",
            path,
            json={"expected_revision": 2},
            headers={"Authorization": "Bearer delete-test"},
        )
        accepted = await client.request(
            "DELETE",
            path,
            json={"expected_revision": 2},
            headers={"Authorization": "Bearer delete-test", "Origin": "http://localhost"},
        )
    assert refused.status_code == 403
    assert accepted.status_code == 202
    assert accepted.json()["id"] == str(session.conversation.id)
