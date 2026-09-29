"""Atomic owner admission for a Studio coding run started from Chat."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import Request
from opentelemetry import metrics, trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api import mcp_calls, runs
from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.chat.turns import Admission, project_turn, request_hash
from coire_api.coding_calls import create_coding_call, prepare_chat_coding_request
from coire_api.db import (
    AgentRunRow,
    ChatConversationRow,
    ChatEventRow,
    ChatMessageRow,
    ChatTurnRow,
    McpCallRow,
    ModelRow,
)
from coire_api.registry.service import chat_model_eligible
from coire_core.errors import ChatConflict, ChatNotFound
from coire_core.models.chat import (
    ChatEvent,
    ChatStopRequest,
    ChatTurnAccepted,
    ChatTurnCreate,
    ChatTurnResult,
    ChatTurnTerminal,
)
from coire_core.models.harness import ProfileName, TaskClass
from coire_core.models.mcp import (
    ApplyResult,
    McpCallState,
    McpToolName,
    PlanResult,
    ResearchResult,
)
from coire_core.models.runs import AgentRunCreate, AgentRunState, RunLimits
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.chat.coding")
coding_actions_total = metrics.get_meter("coire.api.chat.coding").create_counter(
    "coire_chat_coding_actions_total", unit="1"
)


async def coding_turn_stream(
    admission: Admission, principal: Principal, request: Request, settings: Settings
) -> AsyncIterator[bytes]:
    """Follow the durable run and stop it if its controlling browser POST disconnects."""
    from coire_api.chat.streaming import replay_saved_events
    from coire_api.chat.turns import request_turn_stop
    from coire_api.db import session_scope

    try:
        async for chunk in replay_saved_events(admission, principal, request, settings):
            yield chunk
    finally:
        if await request.is_disconnected():
            try:

                async def stop() -> None:
                    async with session_scope() as session:
                        await request_turn_stop(
                            session,
                            principal,
                            admission.turn.conversation_id,
                            admission.turn.id,
                            ChatStopRequest(reason="navigation"),
                            settings,
                        )

                stop_task = asyncio.create_task(stop())
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(stop_task)
            except Exception as exc:
                logger.error(
                    "chat coding disconnect stop failed run_id=%s error_type=%s",
                    admission.turn.run_id,
                    type(exc).__name__,
                )


async def admit_coding_turn(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    principal: Principal,
    body: ChatTurnCreate,
    settings: Settings,
) -> Admission:
    """Commit Chat turn, coding call and run together before any Studio I/O."""
    conversation = await session.scalar(
        select(ChatConversationRow)
        .where(ChatConversationRow.id == conversation_id)
        .with_for_update()
    )
    if (
        conversation is None
        or conversation.owner_user_id != principal.user_id
        or conversation.deleted_at is not None
    ):
        raise ChatNotFound()
    digest = request_hash(body)
    existing = await session.scalar(
        select(ChatTurnRow).where(
            ChatTurnRow.conversation_id == conversation_id,
            ChatTurnRow.client_request_id == body.client_request_id,
        )
    )
    if existing is not None:
        if existing.request_hash != digest:
            raise ChatConflict("request ID already used with different content")
        return Admission(existing, None, [], 0, True)
    if conversation.revision != body.expected_revision:
        raise ChatConflict("conversation changed; reload before sending")
    if conversation.active_turn_id is not None:
        raise ChatConflict("a turn is already active")
    if conversation.mode != "code" or body.action == "chat":
        raise ChatConflict("code action requires a code conversation")
    if body.retry_of is not None:
        raise ChatConflict("coding recovery requires a fresh explicit action")
    model = await session.get(ModelRow, body.model_id)
    if model is None or not chat_model_eligible(model, principal):
        raise ChatNotFound()
    coding = await prepare_chat_coding_request(session, principal, body)
    assert principal.user_id is not None
    call = await create_coding_call(
        session,
        owner_user_id=principal.user_id,
        credential_id=principal.api_key_id,
        request=coding,
        model_id=body.model_id,
    )
    task_class = TaskClass.WRITE if coding.tool is McpToolName.APPLY else TaskClass.READ
    try:
        run = await runs.create_run(
            session,
            AgentRunCreate(
                profile=ProfileName.CODING,
                primary_model_id=body.model_id,
                workspace_ref=f"pending-{call.id.hex}",
                task_class=task_class,
                prepared_request_id=call.id,
                permitted_model_ids=frozenset({body.model_id}),
                limits=RunLimits(timeout_seconds=settings.mcp_run_timeout_seconds),
            ),
            requester_user_id=principal.user_id,
        )
    except runs.RunConflict as exc:
        raise ChatConflict(str(exc)) from exc
    last_position = await session.scalar(
        select(ChatMessageRow.position)
        .where(ChatMessageRow.conversation_id == conversation_id)
        .order_by(ChatMessageRow.position.desc())
        .limit(1)
    )
    position = (last_position or 0) + 1
    now = datetime.now(UTC)
    input_id, assistant_id, turn_id = (uuid.uuid4() for _ in range(3))
    session.add_all(
        [
            ChatMessageRow(
                id=input_id,
                conversation_id=conversation_id,
                position=position,
                role="user",
                text=body.content,
                prompt_content=body.content,
                reasoning="",
                model_id=model.id,
                model_display_name=model.display_name,
                attachment_ids=[],
                attachment_selections=[],
                created_at=now,
            ),
            ChatMessageRow(
                id=assistant_id,
                conversation_id=conversation_id,
                position=position + 1,
                role="assistant",
                text="",
                reasoning="",
                model_id=model.id,
                model_display_name=model.display_name,
                attachment_ids=[],
                created_at=now,
            ),
        ]
    )
    await session.flush()
    turn = ChatTurnRow(
        id=turn_id,
        conversation_id=conversation_id,
        client_request_id=body.client_request_id,
        request_hash=digest,
        accepted_revision=body.expected_revision,
        input_message_id=input_id,
        assistant_message_id=assistant_id,
        model_id=model.id,
        model_display_name=model.display_name,
        action=coding.tool.value,
        state="accepted",
        coding_call_id=call.id,
        run_id=run.id,
        event_cursor=conversation.event_cursor + 1,
        created_at=now,
        updated_at=now,
    )
    session.add(turn)
    await session.flush()
    event = ChatEvent(
        conversation_id=conversation_id,
        cursor=conversation.event_cursor + 1,
        turn_id=turn_id,
        created_at=now,
        payload=ChatTurnAccepted(turn=project_turn(turn)),
    )
    session.add(
        ChatEventRow(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            turn_id=turn_id,
            cursor=event.cursor,
            type=event.payload.type,
            payload=event.payload.model_dump(mode="json"),
            created_at=now,
            expires_at=now + timedelta(hours=settings.chat_event_retention_hours),
        )
    )
    conversation.active_turn_id = turn_id
    conversation.selected_model_id = model.id
    conversation.revision += 1
    conversation.event_cursor += 1
    conversation.updated_at = now
    await write_principal_audit(
        session,
        principal=principal,
        action=f"chat.{coding.tool.value}",
        target_type="chat_turn",
        target_id=str(turn_id),
        detail={"run_id": str(run.id), "model_id": str(model.id)},
    )
    await session.commit()
    coding_actions_total.add(1, {"action": coding.tool.value, "outcome": "accepted"})
    return Admission(turn, event, [], 0, False)


def _coding_summary(result: ResearchResult | PlanResult | ApplyResult) -> str:
    if isinstance(result, ResearchResult):
        return result.answer
    if isinstance(result, PlanResult):
        return result.goal + "\n" + "\n".join(step.description for step in result.steps)
    return f"Apply completed on {result.branch}. Tests: {result.tests.status.value}."


async def reconcile_chat_coding_result(run_id: uuid.UUID, settings: Settings) -> bool:
    """Publish one typed result and terminal event after the durable run finishes."""
    with tracer.start_as_current_span("coire.api.chat.coding.reconcile") as span:
        span.set_attribute("run_id", str(run_id))
        return await _reconcile_chat_coding_result(run_id, settings)


async def _reconcile_chat_coding_result(run_id: uuid.UUID, settings: Settings) -> bool:
    from coire_api.db import session_scope

    async with session_scope() as session:
        turn_ref = await session.scalar(select(ChatTurnRow).where(ChatTurnRow.run_id == run_id))
        if turn_ref is None:
            return False
        conversation = await session.scalar(
            select(ChatConversationRow)
            .where(ChatConversationRow.id == turn_ref.conversation_id)
            .with_for_update()
        )
        if conversation is None or conversation.deleted_at is not None:
            return False
        turn = await session.get(ChatTurnRow, turn_ref.id, with_for_update=True)
        run = await session.get(AgentRunRow, run_id)
        if turn is None or run is None or turn.run_id != run_id or turn.coding_call_id is None:
            raise ValueError("coding result identity is incomplete")
        if turn.state in {"completed", "failed", "stopped", "interrupted"}:
            return False
        call = await session.get(McpCallRow, turn.coding_call_id, with_for_update=True)
        if (
            call is None
            or call.run_id != run_id
            or call.owner_user_id != conversation.owner_user_id
            or run.requester_user_id != conversation.owner_user_id
            or run.prepared_request_id != call.id
            or call.tool.value != turn.action
        ):
            raise ValueError("coding result is not owned by this Chat turn")
        if run.state not in {
            AgentRunState.SUCCEEDED,
            AgentRunState.FAILED,
            AgentRunState.TIMED_OUT,
            AgentRunState.RESULT_COLLECTION_FAILED,
            AgentRunState.KILLED,
        }:
            return False
        result: ResearchResult | PlanResult | ApplyResult | None = None
        if run.state is AgentRunState.SUCCEEDED and run.result is not None:
            output = run.result.get("output")
            if isinstance(output, dict):
                try:
                    result = (
                        ResearchResult.model_validate(output)
                        if call.tool is McpToolName.RESEARCH
                        else PlanResult.model_validate(output)
                        if call.tool is McpToolName.PLAN
                        else ApplyResult.model_validate(output)
                    )
                except ValueError:
                    result = None
            if result is not None and result.run_id != run_id:
                result = None
        now = datetime.now(UTC)

        def append_event(payload: ChatTurnResult | ChatTurnTerminal) -> None:
            conversation.event_cursor += 1
            session.add(
                ChatEventRow(
                    id=uuid.uuid4(),
                    conversation_id=conversation.id,
                    turn_id=turn.id,
                    cursor=conversation.event_cursor,
                    type=payload.type,
                    payload=payload.model_dump(mode="json"),
                    created_at=now,
                    expires_at=now + timedelta(hours=settings.chat_event_retention_hours),
                )
            )

        answer = await session.get(ChatMessageRow, turn.assistant_message_id)
        if answer is None:
            raise ValueError("coding assistant message is absent")
        state: Literal["completed", "failed", "stopped"]
        if result is not None:
            await mcp_calls.store_result(session, row=call, result=result)
            answer.text = _coding_summary(result)[: 512 * 1024]
            append_event(ChatTurnResult(tool=call.tool, result=result))
            state = "completed"
        else:
            state = "stopped" if run.state is AgentRunState.KILLED else "failed"
            if call.state not in {
                McpCallState.SUCCEEDED,
                McpCallState.FAILED,
                McpCallState.TIMED_OUT,
                McpCallState.CANCELLED,
            }:
                call_state = (
                    McpCallState.CANCELLED
                    if state == "stopped"
                    else McpCallState.TIMED_OUT
                    if run.state is AgentRunState.TIMED_OUT
                    else McpCallState.FAILED
                )
                await mcp_calls.fail_call(
                    session,
                    row=call,
                    state=call_state,
                    code=run.failure_code or "coding_run_failed",
                )
        turn.state = state
        turn.updated_at = now
        if conversation.active_turn_id == turn.id:
            conversation.active_turn_id = None
        conversation.updated_at = now
        append_event(
            ChatTurnTerminal(
                state=state,
                answer_length=len(answer.text),
                reasoning_length=0,
                safe_error=None if result is not None else "coding run did not complete",
            )
        )
        coding_actions_total.add(1, {"action": call.tool.value, "outcome": state})
        logger.info("chat coding reconciled run_id=%s state=%s", run_id, state)
        return True
