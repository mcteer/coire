"""Coding receipts are owner-bound and idempotent before replay."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from coire_api.chat import activity
from coire_api.db import AgentRunRow, ChatConversationRow, ChatEventRow, ChatTurnRow
from coire_core.models.runs import RunActivity, RunActivityPage, RunActivityTool
from coire_core.settings import Settings


def _receipt(run_id: uuid.UUID, sequence: int) -> RunActivity:
    return RunActivity(
        run_id=run_id,
        sequence=sequence,
        tool_name=RunActivityTool.READ_FILE,
        state="completed",
        created_at=datetime.now(UTC),
    )


class Session:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.run_id = uuid.uuid4()
        self.owner_id = uuid.uuid4()
        self.call_id = uuid.uuid4()
        self.conversation = ChatConversationRow(
            id=uuid.uuid4(),
            owner_user_id=self.owner_id,
            title="Private",
            mode="code",
            revision=1,
            event_cursor=4,
            created_at=now,
            updated_at=now,
        )
        self.turn = ChatTurnRow(
            id=uuid.uuid4(),
            conversation_id=self.conversation.id,
            run_id=self.run_id,
            coding_call_id=self.call_id,
            client_request_id=uuid.uuid4(),
            request_hash="a" * 64,
            accepted_revision=1,
            input_message_id=uuid.uuid4(),
            assistant_message_id=uuid.uuid4(),
            model_id=uuid.uuid4(),
            model_display_name="Model",
            action="research",
            state="running",
            activity_sequence=0,
            created_at=now,
            updated_at=now,
        )
        self.run = AgentRunRow(
            id=self.run_id,
            requester_user_id=self.owner_id,
            prepared_request_id=self.call_id,
        )
        self.events: list[ChatEventRow] = []

    async def scalar(self, statement: object) -> object:
        query = str(statement)
        if query.startswith("SELECT chat_turns.activity_sequence "):
            return self.turn.activity_sequence if self.turn.run_id is not None else None
        if "FROM chat_conversations" in query:
            return self.conversation
        return self.turn

    async def get(self, model: type, _id: uuid.UUID) -> object:
        return self.run if model is AgentRunRow else None

    def add(self, row: ChatEventRow) -> None:
        self.events.append(row)


async def test_activity_page_persists_once_and_rejects_foreign_or_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield session

    monkeypatch.setattr(activity, "session_scope", sessions)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    page = RunActivityPage(
        run_id=session.run_id,
        data=[_receipt(session.run_id, 1), _receipt(session.run_id, 2)],
    )
    assert await activity.persist_activity_page(session.run_id, page, settings) == 2
    assert await activity.persist_activity_page(session.run_id, page, settings) == 0
    assert session.turn.activity_sequence == 2
    assert session.conversation.event_cursor == 6
    assert [row.type for row in session.events] == ["run.activity", "run.activity"]
    assert [row.payload["activity"]["sequence"] for row in session.events] == [1, 2]  # type: ignore[index]
    with pytest.raises(ValueError, match="sequence"):
        await activity.persist_activity_page(
            session.run_id,
            RunActivityPage(run_id=session.run_id, data=[_receipt(session.run_id, 4)]),
            settings,
        )
    session.run.requester_user_id = uuid.uuid4()
    with pytest.raises(ValueError, match="owned"):
        await activity.persist_activity_page(
            session.run_id,
            RunActivityPage(run_id=session.run_id, data=[_receipt(session.run_id, 3)]),
            settings,
        )
    session.run.requester_user_id = session.owner_id
    assert await activity.persist_activity_final_status(session.run_id, "truncated", settings)
    assert not await activity.persist_activity_final_status(session.run_id, "complete", settings)
    assert session.conversation.event_cursor == 7
    assert session.events[-1].type == "run.activity_status"
    assert session.events[-1].payload["last_sequence"] == 2
    assert session.events[-1].payload["state"] == "truncated"
    assert (
        await activity.persist_activity_page(
            session.run_id,
            RunActivityPage(run_id=session.run_id, data=[_receipt(session.run_id, 3)]),
            settings,
        )
        == 0
    )


async def test_plain_mcp_run_has_no_chat_activity_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Session()
    session.turn.run_id = None

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield session

    monkeypatch.setattr(activity, "session_scope", sessions)
    assert await activity.activity_cursor(session.run_id) is None


async def test_collector_resumes_from_durable_sequence_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    cursors: list[int] = []

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield session

    class Client:
        def __init__(self, _settings: Settings) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def run_activity(
            self, _node: str, run_id: uuid.UUID, *, after_sequence: int
        ) -> RunActivityPage:
            cursors.append(after_sequence)
            return RunActivityPage(
                run_id=run_id,
                data=[_receipt(run_id, 1), _receipt(run_id, 2)] if after_sequence == 0 else [],
            )

    monkeypatch.setattr(activity, "session_scope", sessions)
    monkeypatch.setattr(activity, "NodeClient", Client)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert await activity.collect_run_activity(session.run_id, "edge-a", settings) == 2
    assert await activity.collect_run_activity(session.run_id, "edge-a", settings) == 0
    assert cursors == [0, 2]
    assert len(session.events) == 2
