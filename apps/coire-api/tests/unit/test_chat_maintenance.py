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
