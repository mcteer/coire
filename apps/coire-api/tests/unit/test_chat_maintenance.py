"""Expired plain-chat leases reconcile to one durable terminal event."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from coire_api.chat import maintenance
from coire_api.db import ChatConversationRow, ChatEventRow, ChatMessageRow, ChatTurnRow
from coire_core.settings import Settings


class RecoverySession:
    def __init__(self, *, stopped: bool = False, fresh: bool = False) -> None:
        now = datetime.now(UTC)
        self.conversation = ChatConversationRow(
            id=uuid.uuid4(),
            owner_user_id=uuid.uuid4(),
            title="Saved",
            mode="chat",
            revision=2,
            event_cursor=3,
            created_at=now,
            updated_at=now,
        )
        self.answer = ChatMessageRow(
            id=uuid.uuid4(),
            conversation_id=self.conversation.id,
            position=2,
            role="assistant",
            text="Partial answer",
            reasoning="",
            attachment_ids=[],
            created_at=now,
        )
        self.turn = ChatTurnRow(
            id=uuid.uuid4(),
            conversation_id=self.conversation.id,
            client_request_id=uuid.uuid4(),
            request_hash="a" * 64,
            accepted_revision=1,
            input_message_id=uuid.uuid4(),
            assistant_message_id=self.answer.id,
            model_id=uuid.uuid4(),
            model_display_name="Saved model",
            action="chat",
            state="stop_requested" if stopped else "running",
            owner_process="dead-process",
            lease_expires_at=now + timedelta(seconds=20 if fresh else -20),
            created_at=now - timedelta(minutes=1),
            updated_at=now - timedelta(minutes=1),
        )
        self.conversation.active_turn_id = self.turn.id
        self.events: list[ChatEventRow] = []

    async def execute(self, _statement: object) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: [(self.turn.id, self.conversation.id)])

    async def scalar(self, _statement: object) -> ChatConversationRow:
        return self.conversation

    async def get(self, model: type, _identifier: uuid.UUID, **_kwargs: object) -> object:
        if model is ChatTurnRow:
            return self.turn
        if model is ChatMessageRow:
            return self.answer
        return None

    def add(self, row: ChatEventRow) -> None:
        self.events.append(row)


@pytest.mark.parametrize("stop", [False, True])
async def test_expired_turn_recovery_preserves_partial_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, stop: bool
) -> None:
    session = RecoverySession(stopped=stop)

    @asynccontextmanager
    async def sessions() -> AsyncIterator[RecoverySession]:
        yield session

    monkeypatch.setattr(maintenance, "session_scope", sessions)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert await maintenance.sweep_stale_turns(settings) == 1
    assert await maintenance.sweep_stale_turns(settings) == 0
    assert session.turn.state == ("stopped" if stop else "interrupted")
    assert session.conversation.active_turn_id is None
    assert session.conversation.event_cursor == 4
    assert len(session.events) == 1
    payload = session.events[0].payload
    assert payload["answer_length"] == len("Partial answer")
    assert payload["state"] == session.turn.state
    assert "Partial answer" not in str(payload)


async def test_renewed_lease_is_not_reconciled(monkeypatch: pytest.MonkeyPatch) -> None:
    session = RecoverySession(fresh=True)

    @asynccontextmanager
    async def sessions() -> AsyncIterator[RecoverySession]:
        yield session

    monkeypatch.setattr(maintenance, "session_scope", sessions)
    assert await maintenance.sweep_stale_turns(Settings(_secrets_dir="/nonexistent")) == 0  # type: ignore[call-arg]
    assert session.turn.state == "running"
    assert session.events == []


@pytest.mark.parametrize("blocker", [None, "attachment", "active"])
async def test_text_purge_scrubs_only_safe_tombstones(
    monkeypatch: pytest.MonkeyPatch, blocker: str | None
) -> None:
    now = datetime.now(UTC)
    conversation = ChatConversationRow(
        id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        title="Sensitive title",
        mode="chat",
        selected_model_id=uuid.uuid4(),
        revision=3,
        event_cursor=4,
        deleted_at=now - timedelta(minutes=10),
        created_at=now,
        updated_at=now,
    )
    conversation.active_turn_id = uuid.uuid4()
    commands: list[str] = []

    class PurgeSession:
        async def execute(self, statement: object) -> SimpleNamespace:
            commands.append(str(statement))
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [conversation.id]))

        async def scalar(self, statement: object) -> object:
            sql = str(statement)
            if "FROM chat_conversations" in sql:
                return conversation
            if "FROM chat_attachments" in sql and blocker == "attachment":
                return uuid.uuid4()
            if "FROM chat_turns" in sql and blocker == "active":
                return uuid.uuid4()
            return None

        async def flush(self) -> None:
            return None

    @asynccontextmanager
    async def sessions() -> AsyncIterator[PurgeSession]:
        yield PurgeSession()

    monkeypatch.setattr(maintenance, "session_scope", sessions)
    count = await maintenance.purge_deleted_text()
    if blocker is None:
        assert count == 1
        assert conversation.title == "Deleted conversation"
        assert conversation.selected_model_id is None
        assert conversation.active_turn_id is None
        assert conversation.purged_at is not None
        assert sum(command.startswith("DELETE FROM chat_") for command in commands) == 4
        assert await maintenance.purge_deleted_text() == 0
    else:
        assert count == 0
        assert conversation.purged_at is None
        assert not any(command.startswith("DELETE FROM chat_") for command in commands)


async def test_expired_event_compaction_is_bounded_and_history_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [uuid.uuid4(), uuid.uuid4()]
    statements: list[str] = []

    class Session:
        async def execute(self, statement: object) -> SimpleNamespace:
            statements.append(str(statement))
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: events))

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(maintenance, "session_scope", sessions)
    assert await maintenance.compact_expired_events() == 2
    assert "LIMIT" in statements[0] and "chat_events.expires_at" in statements[0]
    assert statements[1].startswith("DELETE FROM chat_events")
    assert not any("chat_messages" in statement for statement in statements)


async def test_oldest_pending_purge_metric_tracks_overdue_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[float] = []

    class Session:
        async def scalar(self, _statement: object) -> datetime:
            return datetime.now(UTC) - timedelta(hours=25)

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(maintenance, "session_scope", sessions)
    monkeypatch.setattr(
        maintenance, "purge_oldest_seconds", SimpleNamespace(set=recorded.append)
    )
    age = await maintenance.record_oldest_pending_purge()
    assert age > 24 * 3600
    assert recorded == [age]
