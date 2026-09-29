"""A Chat coding turn is admitted and finalized as one durable owner run."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api import mcp_calls, runs
from coire_api.auth import Principal, PrincipalKind
from coire_api.chat import coding
from coire_api.chat.turns import read_turn_detail
from coire_api.coding_calls import CodingRequest
from coire_api.db import (
    AgentRunRow,
    ChatConversationRow,
    ChatEventRow,
    ChatMessageRow,
    ChatTurnRow,
    McpCallRow,
    ModelRow,
)
from coire_core.errors import ChatConflict, ChatNotFound
from coire_core.models.chat import ChatTurnCreate
from coire_core.models.mcp import (
    ApplyInput,
    McpCallState,
    McpToolName,
    ResearchInput,
    ResearchResult,
    WorkspaceSource,
)
from coire_core.models.runs import AgentRunState
from coire_core.settings import Settings


class Session:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.owner_id = uuid.uuid4()
        self.conversation = ChatConversationRow(
            id=uuid.uuid4(),
            owner_user_id=self.owner_id,
            title="Code",
            mode="code",
            revision=1,
            event_cursor=0,
            created_at=now,
            updated_at=now,
        )
        self.model = ModelRow(
            id=uuid.uuid4(), display_name="Coding", capability_profile={}, context_window=4096
        )
        self.call = McpCallRow(
            id=uuid.uuid4(),
            owner_user_id=self.owner_id,
            tool=McpToolName.RESEARCH,
            state=McpCallState.QUEUED,
            model_id=self.model.id,
        )
        self.run = AgentRunRow(
            id=uuid.uuid4(),
            requester_user_id=self.owner_id,
            prepared_request_id=self.call.id,
            state=AgentRunState.QUEUED,
        )
        self.call.run_id = self.run.id
        self.turn: ChatTurnRow | None = None
        self.messages: list[ChatMessageRow] = []
        self.events: list[ChatEventRow] = []

    async def scalar(self, statement: object) -> object:
        query = str(statement)
        if "FROM chat_conversations" in query:
            return self.conversation
        if "FROM chat_turns" in query:
            return self.turn
        if "chat_messages.position" in query:
            return None
        raise AssertionError(query)

    async def get(self, model: type, _id: uuid.UUID, **_kwargs: object) -> object:
        if model is ChatMessageRow:
            return next((row for row in self.messages if row.id == _id), None)
        rows: dict[type, object] = {
            ChatConversationRow: self.conversation,
            ModelRow: self.model,
            ChatTurnRow: self.turn,
            AgentRunRow: self.run,
            McpCallRow: self.call,
        }
        row = rows.get(model)
        return (
            row
            if row is not None
            and isinstance(
                row, (ChatConversationRow, ModelRow, ChatTurnRow, AgentRunRow, McpCallRow)
            )
            and row.id == _id
            else None
        )

    def add(self, row: object) -> None:
        if isinstance(row, ChatTurnRow):
            self.turn = row
        if isinstance(row, ChatEventRow):
            self.events.append(row)

    def add_all(self, rows: list[object]) -> None:
        self.messages.extend(cast(list[ChatMessageRow], rows))

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


async def test_chat_coding_admission_is_owner_bound_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    principal = Principal(kind=PrincipalKind.USER, user_id=session.owner_id)
    body = ChatTurnCreate(
        client_request_id=uuid.uuid4(),
        expected_revision=1,
        model_id=session.model.id,
        content="Where is code?",
        action="research",
        workspace_id=uuid.uuid4(),
    )
    source = WorkspaceSource(workspace_id=body.workspace_id, revision="HEAD")

    async def prepare(*_args: object) -> CodingRequest:
        return CodingRequest(
            McpToolName.RESEARCH,
            ResearchInput(source=source, question=body.content, model_id=body.model_id),
            body.content,
        )

    async def create_call(*_args: object, **_kwargs: object) -> McpCallRow:
        return session.call

    async def create_run(*_args: object, **_kwargs: object) -> AgentRunRow:
        return session.run

    async def audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(coding, "prepare_chat_coding_request", prepare)
    monkeypatch.setattr(coding, "create_coding_call", create_call)
    monkeypatch.setattr(runs, "create_run", create_run)
    monkeypatch.setattr(coding, "write_principal_audit", audit)
    monkeypatch.setattr(coding, "chat_model_eligible", lambda *_args: True)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    accepted = await coding.admit_coding_turn(
        cast(AsyncSession, session), session.conversation.id, principal, body, settings
    )
    assert accepted.turn.run_id == session.run.id
    assert accepted.turn.coding_call_id == session.call.id
    assert accepted.event is not None and accepted.event.payload.type == "turn.accepted"
    assert session.conversation.active_turn_id == accepted.turn.id
    assert session.conversation.revision == 2
    assert [row.position for row in session.messages] == [1, 2]
    replay = await coding.admit_coding_turn(
        cast(AsyncSession, session), session.conversation.id, principal, body, settings
    )
    assert replay.replay and replay.turn.id == accepted.turn.id
    assert len(session.messages) == 2
    with pytest.raises(ChatNotFound):
        await coding.admit_coding_turn(
            cast(AsyncSession, session),
            session.conversation.id,
            Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4()),
            body,
            settings,
        )


async def test_apply_admission_uses_existing_verified_write_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_core.models.harness import TaskClass
    from coire_core.models.runs import AgentRunCreate

    session = Session()
    principal = Principal(kind=PrincipalKind.USER, user_id=session.owner_id)
    body = ChatTurnCreate(
        client_request_id=uuid.uuid4(),
        expected_revision=1,
        model_id=session.model.id,
        content="Apply the saved plan",
        action="apply",
        workspace_id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
    )
    source = WorkspaceSource(workspace_id=body.workspace_id, revision="a" * 40)

    async def prepare(*_args: object) -> CodingRequest:
        return CodingRequest(
            McpToolName.APPLY,
            ApplyInput(source=source, plan_result_id=body.plan_id, model_id=body.model_id),
            "plan text",
        )

    async def create_call(*_args: object, **_kwargs: object) -> McpCallRow:
        return session.call

    async def refuse_run(_session: object, request: AgentRunCreate, **_kwargs: object) -> None:
        assert request.task_class is TaskClass.WRITE
        raise runs.RunConflict("every permitted model needs a published harness-verified variant")

    monkeypatch.setattr(coding, "prepare_chat_coding_request", prepare)
    monkeypatch.setattr(coding, "create_coding_call", create_call)
    monkeypatch.setattr(runs, "create_run", refuse_run)
    monkeypatch.setattr(coding, "chat_model_eligible", lambda *_args: True)
    with pytest.raises(ChatConflict, match="harness-verified"):
        await coding.admit_coding_turn(
            cast(AsyncSession, session),
            session.conversation.id,
            principal,
            body,
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    assert session.turn is None and session.conversation.revision == 1


async def test_successful_run_persists_result_then_terminal_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    now = datetime.now(UTC)
    session.run.state = AgentRunState.SUCCEEDED
    result = ResearchResult.model_validate(
        {
            "result_id": str(uuid.uuid4()),
            "run_id": str(session.run.id),
            "source_revision": "a" * 40,
            "answer": "Found main.py",
            "citations": [{"path": "main.py", "line": 1}],
        }
    )
    session.run.result = {"output": result.model_dump(mode="json")}
    session.turn = ChatTurnRow(
        id=uuid.uuid4(),
        conversation_id=session.conversation.id,
        coding_call_id=session.call.id,
        run_id=session.run.id,
        assistant_message_id=uuid.uuid4(),
        action="research",
        state="running",
        activity_sequence=0,
        created_at=now,
        updated_at=now,
    )
    session.messages.append(
        ChatMessageRow(id=session.turn.assistant_message_id, text="", reasoning="")
    )
    session.conversation.active_turn_id = session.turn.id

    @asynccontextmanager
    async def scope() -> AsyncIterator[Session]:
        yield session

    async def store_result(*_args: object, **_kwargs: object) -> None:
        session.call.state = McpCallState.SUCCEEDED

    monkeypatch.setattr("coire_api.db.session_scope", scope)
    monkeypatch.setattr(mcp_calls, "store_result", store_result)
    terminal_metrics: list[dict[str, str]] = []
    monkeypatch.setattr(
        coding,
        "turns_total",
        SimpleNamespace(add=lambda _value, labels: terminal_metrics.append(labels)),
    )
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert await coding.reconcile_chat_coding_result(session.run.id, settings)
    assert not await coding.reconcile_chat_coding_result(session.run.id, settings)
    assert session.turn.state == "completed"
    assert terminal_metrics == [{"mode": "coding", "outcome": "completed"}]
    assert session.messages[0].text == "Found main.py"
    assert [row.type for row in session.events] == ["turn.result", "turn.terminal"]
    input_id = uuid.uuid4()
    session.messages.append(
        ChatMessageRow(
            id=input_id,
            conversation_id=session.conversation.id,
            position=1,
            role="user",
            text="Where?",
            reasoning="",
            attachment_ids=[],
            attachment_selections=[],
            created_at=now,
        )
    )
    session.messages[0].conversation_id = session.conversation.id
    session.messages[0].position = 2
    session.messages[0].role = "assistant"
    session.messages[0].attachment_ids = []
    session.messages[0].attachment_selections = []
    session.messages[0].created_at = now
    session.turn.client_request_id = uuid.uuid4()
    session.turn.accepted_revision = 1
    session.turn.input_message_id = input_id
    session.turn.model_id = session.model.id
    session.turn.model_display_name = "Coding"
    session.call.result = result.model_dump(mode="json")
    detail = await read_turn_detail(
        cast(AsyncSession, session),
        Principal(kind=PrincipalKind.USER, user_id=session.owner_id),
        session.conversation.id,
        session.turn.id,
    )
    assert isinstance(detail.coding_result, ResearchResult)
    assert detail.coding_result.answer == "Found main.py"
    assert session.conversation.active_turn_id is None


async def test_failed_run_persists_safe_terminal_and_fails_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    now = datetime.now(UTC)
    session.run.state = AgentRunState.FAILED
    session.run.failure_code = "engine_unavailable"
    session.turn = ChatTurnRow(
        id=uuid.uuid4(),
        conversation_id=session.conversation.id,
        coding_call_id=session.call.id,
        run_id=session.run.id,
        assistant_message_id=uuid.uuid4(),
        action="research",
        state="running",
        created_at=now,
        updated_at=now,
    )
    session.messages.append(
        ChatMessageRow(id=session.turn.assistant_message_id, text="", reasoning="")
    )
    session.conversation.active_turn_id = session.turn.id

    @asynccontextmanager
    async def scope() -> AsyncIterator[Session]:
        yield session

    async def fail_call(*_args: object, **_kwargs: object) -> None:
        session.call.state = McpCallState.FAILED

    monkeypatch.setattr("coire_api.db.session_scope", scope)
    monkeypatch.setattr(mcp_calls, "fail_call", fail_call)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert await coding.reconcile_chat_coding_result(session.run.id, settings)
    assert not await coding.reconcile_chat_coding_result(session.run.id, settings)
    assert session.call.state is McpCallState.FAILED
    assert session.turn.state == "failed"
    assert [row.type for row in session.events] == ["turn.terminal"]
    assert session.events[0].payload["safe_error"] == "coding run did not complete"


async def test_controlling_coding_stream_requests_stop_on_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.chat import streaming, turns
    from coire_api.chat.turns import Admission

    session = Session()
    session.turn = ChatTurnRow(
        id=uuid.uuid4(), conversation_id=session.conversation.id, run_id=session.run.id
    )
    stopped: list[uuid.UUID] = []

    async def replay(*_args: object) -> AsyncIterator[bytes]:
        yield b"event: turn.accepted\n\n"

    async def stop(
        _session: object,
        _principal: object,
        _conversation_id: uuid.UUID,
        turn_id: uuid.UUID,
        *_args: object,
    ) -> None:
        stopped.append(turn_id)

    @asynccontextmanager
    async def scope() -> AsyncIterator[Session]:
        yield session

    monkeypatch.setattr(streaming, "replay_saved_events", replay)
    monkeypatch.setattr(turns, "request_turn_stop", stop)
    monkeypatch.setattr("coire_api.db.session_scope", scope)
    chunks = [
        chunk
        async for chunk in coding.coding_turn_stream(
            Admission(session.turn, None, [], 0, False),
            Principal(kind=PrincipalKind.USER, user_id=session.owner_id),
            cast(Request, SimpleNamespace(is_disconnected=AsyncMock(return_value=True))),
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    ]
    assert chunks == [b"event: turn.accepted\n\n"]
    assert stopped == [session.turn.id]
