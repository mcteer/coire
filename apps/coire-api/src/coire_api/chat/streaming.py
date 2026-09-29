"""Persist-before-emit native text events over the existing gateway proxy."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Literal

from fastapi import Request
from sqlalchemy import select

from coire_api.auth import Principal
from coire_api.chat.maintenance import maintain_turn_lease
from coire_api.chat.reasoning import ReasoningParser
from coire_api.chat.telemetry import parser_failures_total, requests_total, tracer
from coire_api.chat.turns import Admission
from coire_api.db import (
    ChatConversationRow,
    ChatEventRow,
    ChatMessageRow,
    ChatTurnRow,
    EngineProcessRow,
    EntitlementRow,
    ModelInstanceRow,
    ModelRow,
    UserRow,
    session_scope,
)
from coire_api.gateway.execution import (
    cancel_pending_load,
    canonical_text_payload,
    load_with_ceiling,
    track_stream,
)
from coire_api.gateway.proxy import StreamTiming, stream
from coire_api.gateway.resolution import ResolvedModel, resolve_model
from coire_api.gateway.usage import UsageTracker
from coire_api.registry.service import chat_model_eligible
from coire_core.errors import ChatConflict, ChatNotFound
from coire_core.models.chat import (
    ChatEvent,
    ChatMessageDelta,
    ChatMessagePageQuery,
    ChatSnapshot,
    ChatTurnStatus,
    ChatTurnTerminal,
    ChatUsage,
)
from coire_core.models.gateway import GatewayProtocol, UsageOutcome
from coire_core.models.instance import InstanceState
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


class ChatStopRequested(Exception):
    """The controlling generator saw a durable owner Stop request."""


def encode_event(event: ChatEvent) -> bytes:
    return (
        f"event: {event.payload.type}\nid: {event.event_id}\ndata: {event.model_dump_json()}\n\n"
    ).encode()


async def persist_native_event(
    kind: Literal["status", "delta", "terminal"],
    admission: Admission,
    *,
    state: str | None = None,
    text: str | None = None,
    channel: Literal["answer", "reasoning"] = "answer",
    usage: ChatUsage | None = None,
    safe_error: str | None = None,
    estimate_seconds: float | None = None,
    settings: Settings,
) -> ChatEvent:
    """Commit state and event atomically, then return bytes to the streaming caller."""
    turn_id = admission.turn.id
    conversation_id = admission.turn.conversation_id
    async with session_scope() as session:
        conversation = await session.scalar(
            select(ChatConversationRow)
            .where(ChatConversationRow.id == conversation_id)
            .with_for_update()
        )
        if conversation is None or conversation.deleted_at is not None:
            raise ChatNotFound()
        turn = await session.get(ChatTurnRow, turn_id)
        if turn is None or turn.conversation_id != conversation_id:
            raise ChatNotFound()
        answer = await session.get(ChatMessageRow, turn.assistant_message_id)
        if answer is None:
            raise ChatNotFound()
        if turn.state in {"completed", "failed", "stopped", "interrupted"}:
            raise ChatConflict("turn is already terminal")
        if turn.state == "stop_requested" and (kind != "terminal" or state != "stopped"):
            raise ChatStopRequested()
        now = datetime.now(UTC)
        payload: ChatTurnStatus | ChatMessageDelta | ChatTurnTerminal
        if kind == "status":
            assert state in {"queued", "loading", "running"}
            turn.state = state
            payload = ChatTurnStatus(state=state, estimate_seconds=estimate_seconds)  # type: ignore[arg-type]
        elif kind == "delta":
            assert text
            current = answer.text if channel == "answer" else answer.reasoning
            if len(current) + len(text) > 512 * 1024:
                raise ChatConflict("assistant output limit exceeded")
            if channel == "answer":
                answer.text += text
            else:
                answer.reasoning += text
            payload = ChatMessageDelta(
                message_id=answer.id, channel=channel, text=text, offset=len(current) + len(text)
            )
        else:
            assert state in {"completed", "failed", "interrupted", "stopped"}
            turn.state = state
            turn.finished_at = now
            turn.owner_process = None
            turn.lease_expires_at = None
            turn.usage = usage.model_dump(mode="json") if usage is not None else None
            answer.usage = turn.usage
            conversation.active_turn_id = None
            payload = ChatTurnTerminal(
                state=state,  # type: ignore[arg-type]
                usage=usage,
                answer_length=len(answer.text),
                reasoning_length=len(answer.reasoning),
                safe_error=safe_error,
            )
        turn.updated_at = now
        conversation.event_cursor += 1
        conversation.updated_at = now
        event = ChatEvent(
            conversation_id=conversation_id,
            cursor=conversation.event_cursor,
            turn_id=turn_id,
            created_at=now,
            payload=payload,
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
    if kind == "terminal":
        requests_total.add(1, {"operation": "send_terminal", "outcome": state or "unknown"})
        logger.info(
            "chat turn terminal turn_id=%s model_id=%s state=%s",
            turn_id,
            admission.turn.model_id,
            state,
        )
    return event


async def _resolve(admission: Admission, principal: Principal) -> ResolvedModel:
    async with session_scope() as session:
        return await resolve_model(session, admission.turn.model_id, principal)


async def _measured_warmup_seconds(model_id: uuid.UUID) -> float | None:
    async with session_scope() as session:
        return await session.scalar(
            select(EngineProcessRow.load_seconds)
            .where(
                EngineProcessRow.model_id == model_id,
                EngineProcessRow.load_seconds.is_not(None),
                EngineProcessRow.load_seconds >= 0,
            )
            .order_by(EngineProcessRow.started_at.desc())
            .limit(1)
        )


async def _observed_load_state(model_id: uuid.UUID) -> Literal["queued", "loading"] | None:
    """Report only a real placement transition; no inferred queue rank or ETA."""
    async with session_scope() as session:
        state = await session.scalar(
            select(ModelInstanceRow.state)
            .where(
                ModelInstanceRow.model_id == model_id,
                ModelInstanceRow.state.in_(
                    [
                        InstanceState.REQUESTED,
                        InstanceState.RESERVING,
                        InstanceState.LAUNCHING,
                        InstanceState.WARMING,
                    ]
                ),
            )
            .order_by(ModelInstanceRow.created_at.desc())
            .limit(1)
        )
    if state in {InstanceState.REQUESTED, InstanceState.RESERVING}:
        return "queued"
    if state in {InstanceState.LAUNCHING, InstanceState.WARMING}:
        return "loading"
    return None


async def _stop_requested(turn_id: uuid.UUID) -> bool:
    async with session_scope() as session:
        state = await session.scalar(select(ChatTurnRow.state).where(ChatTurnRow.id == turn_id))
        return state == "stop_requested"


async def _next_with_stop(
    source: AsyncIterator[bytes], turn_id: uuid.UUID, signal: asyncio.Event
) -> bytes:
    async def read_next() -> bytes:
        return await anext(source)

    pending = asyncio.create_task(read_next())
    try:
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(pending), timeout=0.5)
            except TimeoutError:
                if await _stop_requested(turn_id):
                    signal.set()
                    pending.cancel()
                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                        await pending
                    raise ChatStopRequested() from None
    finally:
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending


async def _ensure_current_access(principal: Principal, model_id: uuid.UUID) -> None:
    """Recheck live user/key/entitlements without trusting the stream-start snapshot."""
    if principal.user_id is None:
        raise ChatNotFound()
    async with session_scope() as session:
        user = await session.get(UserRow, principal.user_id)
        model = await session.get(ModelRow, model_id)
        if user is None or not user.active or model is None:
            raise ChatNotFound()
        entitlements = frozenset(
            (
                await session.execute(
                    select(EntitlementRow.name).where(
                        EntitlementRow.user_id == principal.user_id,
                        EntitlementRow.revoked_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        current = principal.model_copy(update={"entitlements": entitlements})
        if not chat_model_eligible(model, current):
            raise ChatNotFound()
        if principal.api_key_id is not None:
            from coire_api.identity.keys import key_is_active

            if not await key_is_active(session, principal):
                raise ChatNotFound()


async def native_stream(
    admission: Admission, principal: Principal, request: Request, settings: Settings
) -> AsyncIterator[bytes]:
    """Stream one admitted turn; duplicate admissions replay their saved events elsewhere."""
    assert admission.event is not None and not admission.replay
    usage = UsageTracker(
        principal,
        str(admission.turn.model_id),
        GatewayProtocol.OPENAI,
        request_id=admission.turn.id,
    )
    usage.prompt_tokens = admission.prompt_tokens
    last_access_check = 0.0
    span = tracer.start_span("coire.api.chat.stream")
    span.set_attribute("chat.turn_id", str(admission.turn.id))
    span.set_attribute("chat.model_id", str(admission.turn.model_id))
    terminal_saved = False
    persistence_failed = False
    loading_failed = False
    stop_signal = asyncio.Event()
    last_stop_check = monotonic()
    heartbeat = asyncio.create_task(maintain_turn_lease(admission.turn.id))
    try:
        yield encode_event(admission.event)
        try:
            await _ensure_current_access(principal, admission.turn.model_id)
            last_access_check = monotonic()
            resolved = await _resolve(admission, principal)
            if resolved.engine_url is None or resolved.model_path is None:
                loading_failed = True
                loading = await persist_native_event(
                    "status",
                    admission,
                    state="loading",
                    estimate_seconds=await _measured_warmup_seconds(admission.turn.model_id),
                    settings=settings,
                )
                yield encode_event(loading)
                last_load_state: Literal["queued", "loading"] = "loading"
                task = asyncio.create_task(load_with_ceiling(admission.turn.model_id, settings))
                last_keepalive = monotonic()
                async with cancel_pending_load(task):
                    while not task.done():
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(task),
                                timeout=min(settings.gateway_keepalive_interval_s, 0.5),
                            )
                        except TimeoutError:
                            if await _stop_requested(admission.turn.id):
                                stop_signal.set()
                                task.cancel()
                                with suppress(asyncio.CancelledError):
                                    await task
                                raise ChatStopRequested() from None
                            await _ensure_current_access(principal, admission.turn.model_id)
                            last_access_check = monotonic()
                            observed = await _observed_load_state(admission.turn.model_id)
                            if observed is not None and observed != last_load_state:
                                last_load_state = observed
                                changed = await persist_native_event(
                                    "status", admission, state=observed, settings=settings
                                )
                                yield encode_event(changed)
                            if (
                                monotonic() - last_keepalive
                                >= settings.gateway_keepalive_interval_s
                            ):
                                last_keepalive = monotonic()
                                yield b": coire model loading\n\n"
                await task
                if await _stop_requested(admission.turn.id):
                    stop_signal.set()
                    raise ChatStopRequested()
                resolved = await _resolve(admission, principal)
            if resolved.engine_url is None or resolved.model_path is None:
                raise RuntimeError("model did not become ready")
            loading_failed = False
            usage.bind_resolution(resolved)
            running = await persist_native_event(
                "status", admission, state="running", settings=settings
            )
            yield encode_event(running)
            payload = canonical_text_payload(
                admission.history, resolved.model_path, output_tokens=admission.output_tokens
            )
            done = False
            failed_frame = False
            reported_usage = False
            parser = ReasoningParser(admission.reasoning_mode)
            timing = StreamTiming()
            tracked = track_stream(
                stream(resolved.engine_url, payload, settings, timing),
                usage,
                request,
                timing,
                stop_signal,
            )
            try:
                while True:
                    try:
                        chunk = await _next_with_stop(tracked, admission.turn.id, stop_signal)
                    except StopAsyncIteration:
                        break
                    if monotonic() - last_stop_check >= 0.5:
                        last_stop_check = monotonic()
                        if await _stop_requested(admission.turn.id):
                            stop_signal.set()
                            raise ChatStopRequested()
                    if monotonic() - last_access_check >= min(
                        settings.credential_stream_recheck_s, 1.0
                    ):
                        await _ensure_current_access(principal, admission.turn.model_id)
                        last_access_check = monotonic()
                    for line in chunk.decode("utf-8", errors="replace").splitlines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if data == "[DONE]":
                            done = True
                            continue
                        try:
                            frame = json.loads(data)
                        except ValueError:
                            failed_frame = True
                            continue
                        if not isinstance(frame, dict):
                            failed_frame = True
                            continue
                        if "error" in frame:
                            failed_frame = True
                            continue
                        reported = frame.get("usage")
                        reported_usage = reported_usage or (
                            isinstance(reported, dict)
                            and isinstance(reported.get("prompt_tokens"), int)
                            and isinstance(reported.get("completion_tokens"), int)
                        )
                        choices = frame.get("choices") or []
                        if not isinstance(choices, list) or not choices:
                            continue
                        delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
                        content = delta.get("content") if isinstance(delta, dict) else None
                        reasoning = (
                            delta.get("reasoning_content") if isinstance(delta, dict) else None
                        )
                        parts: list[tuple[Literal["answer", "reasoning"], str]] = []
                        if admission.reasoning_mode.value != "none" and isinstance(reasoning, str):
                            parts.extend(parser.feed(content) if isinstance(content, str) else [])
                            parts.append(("reasoning", reasoning))
                        elif isinstance(content, str):
                            parts.extend(parser.feed(content))
                        for channel, part in parts:
                            for start in range(0, len(part), 64 * 1024):
                                saved = await persist_native_event(
                                    "delta",
                                    admission,
                                    text=part[start : start + 64 * 1024],
                                    channel=channel,
                                    settings=settings,
                                )
                                yield encode_event(saved)
                for channel, part in parser.finish():
                    for start in range(0, len(part), 64 * 1024):
                        saved = await persist_native_event(
                            "delta",
                            admission,
                            text=part[start : start + 64 * 1024],
                            channel=channel,
                            settings=settings,
                        )
                        yield encode_event(saved)
            finally:
                close = getattr(tracked, "aclose", None)
                if close is not None:
                    await close()
            if not done or failed_frame:
                parser_failures_total.add(
                    1, {"reason": "malformed_frame" if failed_frame else "missing_done"}
                )
                disconnected = await request.is_disconnected()
                state = "interrupted" if disconnected else "failed"
                await usage.finish(
                    UsageOutcome.DISCONNECTED if disconnected else UsageOutcome.FAILED,
                    failure_code="client_disconnected" if disconnected else "engine_stream_failed",
                )
                safe_error = (
                    "connection interrupted"
                    if disconnected
                    else "generation failed; retry this turn"
                )
            else:
                state = "completed"
                safe_error = None
            actual = (
                ChatUsage(
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                )
                if reported_usage
                else None
            )
            terminal = await persist_native_event(
                "terminal",
                admission,
                state=state,
                usage=actual,
                safe_error=safe_error,
                settings=settings,
            )
            terminal_saved = True
            yield encode_event(terminal)
        except asyncio.CancelledError:
            raise
        except ChatStopRequested:
            stop_signal.set()
            await usage.finish(UsageOutcome.STOPPED, failure_code="user_stop")
            try:
                terminal = await persist_native_event(
                    "terminal",
                    admission,
                    state="stopped",
                    safe_error="stopped by user",
                    settings=settings,
                )
            except (ChatNotFound, ChatConflict):
                return
            terminal_saved = True
            yield encode_event(terminal)
        except (ChatNotFound, ChatConflict):
            await usage.finish(UsageOutcome.DISCONNECTED, failure_code="chat_state_changed")
            return
        except Exception as exc:
            logger.error(
                "chat turn failed turn_id=%s model_id=%s error_type=%s",
                admission.turn.id,
                admission.turn.model_id,
                type(exc).__name__,
            )
            await usage.finish(UsageOutcome.FAILED, failure_code="chat_generation_failed")
            try:
                terminal = await persist_native_event(
                    "terminal",
                    admission,
                    state="failed",
                    safe_error=(
                        "model warm-up failed; try again or choose another model"
                        if loading_failed
                        else "generation failed; retry this turn"
                    ),
                    settings=settings,
                )
            except Exception as exc:
                persistence_failed = True
                logger.error(
                    "chat terminal persistence failed turn_id=%s error_type=%s",
                    admission.turn.id,
                    type(exc).__name__,
                )
                return
            terminal_saved = True
            yield encode_event(terminal)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        try:
            if not terminal_saved and not persistence_failed:
                stopped = stop_signal.is_set() or await _stop_requested(admission.turn.id)
                await usage.finish(
                    UsageOutcome.STOPPED if stopped else UsageOutcome.DISCONNECTED,
                    failure_code="user_stop" if stopped else "client_disconnected",
                )
                try:
                    with suppress(ChatNotFound, ChatConflict):
                        await persist_native_event(
                            "terminal",
                            admission,
                            state="stopped" if stopped else "interrupted",
                            safe_error=("stopped by user" if stopped else "connection interrupted"),
                            settings=settings,
                        )
                except Exception as exc:
                    logger.error(
                        "chat terminal persistence failed turn_id=%s error_type=%s",
                        admission.turn.id,
                        type(exc).__name__,
                    )
        finally:
            span.end()


async def replay_saved_events(
    admission: Admission, principal: Principal, request: Request, settings: Settings
) -> AsyncIterator[bytes]:
    """Follow one existing turn without acquiring a second engine or cancellation authority."""
    cursor = 0
    terminal_states = {"completed", "failed", "stopped", "interrupted"}
    while True:
        async with session_scope() as session:
            if principal.user_id is None:
                return
            user = await session.get(UserRow, principal.user_id)
            if user is None or not user.active:
                return
            if principal.api_key_id is not None:
                from coire_api.identity.keys import key_is_active

                if not await key_is_active(session, principal):
                    return
            conversation = await session.get(ChatConversationRow, admission.turn.conversation_id)
            if (
                conversation is None
                or conversation.deleted_at is not None
                or conversation.owner_user_id != principal.user_id
            ):
                return
            rows = (
                (
                    await session.execute(
                        select(ChatEventRow)
                        .where(
                            ChatEventRow.turn_id == admission.turn.id,
                            ChatEventRow.cursor > cursor,
                        )
                        .order_by(ChatEventRow.cursor)
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            turn = await session.get(ChatTurnRow, admission.turn.id)
            finished = turn is None or turn.state in terminal_states
        for row in rows:
            event = ChatEvent.model_validate(
                {
                    "conversation_id": row.conversation_id,
                    "cursor": row.cursor,
                    "turn_id": row.turn_id,
                    "created_at": row.created_at,
                    "payload": row.payload,
                }
            )
            cursor = row.cursor
            yield encode_event(event)
        if finished and len(rows) < 100:
            return
        if await request.is_disconnected():
            return
        try:
            await asyncio.sleep(min(settings.credential_stream_recheck_s, 1.0))
        except asyncio.CancelledError:
            return
        yield b": coire turn active\n\n"


async def observe_conversation(
    conversation_id: uuid.UUID,
    principal: Principal,
    request: Request,
    settings: Settings,
    cursor: int | None,
) -> AsyncIterator[bytes]:
    """Follow saved events without acquiring generation or cancellation authority."""
    from coire_api.chat.service import get_conversation_detail

    while True:
        snapshot = False
        deleted = False
        deleted_event: ChatEventRow | None = None
        rows: list[ChatEventRow] = []
        async with session_scope() as session:
            if principal.user_id is None:
                return
            user = await session.get(UserRow, principal.user_id)
            if user is None or not user.active:
                return
            if principal.api_key_id is not None:
                from coire_api.identity.keys import key_is_active

                if not await key_is_active(session, principal):
                    return
            conversation = await session.get(ChatConversationRow, conversation_id)
            if conversation is None or conversation.owner_user_id != principal.user_id:
                return
            deleted = conversation.deleted_at is not None
            if deleted:
                deleted_event = await session.scalar(
                    select(ChatEventRow)
                    .where(
                        ChatEventRow.conversation_id == conversation_id,
                        ChatEventRow.type == "conversation.deleted",
                    )
                    .order_by(ChatEventRow.cursor.desc())
                    .limit(1)
                )
            elif cursor is None:
                snapshot = conversation.event_cursor > 0
            else:
                rows = list(
                    (
                        await session.execute(
                            select(ChatEventRow)
                            .where(
                                ChatEventRow.conversation_id == conversation_id,
                                ChatEventRow.cursor > cursor,
                            )
                            .order_by(ChatEventRow.cursor)
                            .limit(100)
                        )
                    )
                    .scalars()
                    .all()
                )
                snapshot = bool(rows and rows[0].cursor != cursor + 1) or (
                    not rows and conversation.event_cursor > cursor
                )
            if snapshot:
                detail = await get_conversation_detail(
                    session, principal, conversation_id, ChatMessagePageQuery()
                )
        if deleted:
            if deleted_event is not None and (cursor is None or deleted_event.cursor > cursor):
                event = ChatEvent.model_validate(
                    {
                        "conversation_id": deleted_event.conversation_id,
                        "cursor": deleted_event.cursor,
                        "turn_id": deleted_event.turn_id,
                        "created_at": deleted_event.created_at,
                        "payload": deleted_event.payload,
                    }
                )
                yield encode_event(event)
            return
        if snapshot:
            event = ChatEvent(
                conversation_id=conversation_id,
                cursor=detail.event_cursor,
                created_at=datetime.now(UTC),
                payload=ChatSnapshot(detail=detail, replacement=True),
            )
            cursor = event.cursor
            yield encode_event(event)
        elif cursor is None:
            cursor = 0
        else:
            for row in rows:
                event = ChatEvent.model_validate(
                    {
                        "conversation_id": row.conversation_id,
                        "cursor": row.cursor,
                        "turn_id": row.turn_id,
                        "created_at": row.created_at,
                        "payload": row.payload,
                    }
                )
                cursor = row.cursor
                yield encode_event(event)
        if await request.is_disconnected():
            return
        try:
            await asyncio.sleep(min(settings.credential_stream_recheck_s, 1.0))
        except asyncio.CancelledError:
            return
        yield b": coire conversation active\n\n"
