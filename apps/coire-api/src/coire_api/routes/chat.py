"""Owner-scoped native Chat API."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from coire_api.auth import CurrentChatUser
from coire_api.chat.service import (
    create_conversation,
    get_conversation_detail,
    list_conversations,
    picker,
)
from coire_api.chat.streaming import native_stream, observe_conversation, replay_saved_events
from coire_api.chat.telemetry import requests_total, tracer
from coire_api.chat.turns import admit_turn, read_turn_detail, request_turn_stop
from coire_api.deps import SessionDep, SettingsDep
from coire_core.errors import ChatConflict, ChatModelUnavailable, CoireError
from coire_core.models.chat import (
    ChatConversation,
    ChatConversationCreate,
    ChatConversationDetail,
    ChatConversationPage,
    ChatEvent,
    ChatMessagePageQuery,
    ChatPageQuery,
    ChatPickerQuery,
    ChatPickerResponse,
    ChatStopRequest,
    ChatTurn,
    ChatTurnCreate,
    ChatTurnDetail,
)

logger = logging.getLogger(__name__)


def require_chat_enabled(request: Request) -> None:
    if not request.app.state.settings.chat_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


router = APIRouter(
    prefix="/api/v1/chat", tags=["chat"], dependencies=[Depends(require_chat_enabled)]
)


@router.get("/models", response_model=ChatPickerResponse)
async def list_chat_models(
    query: Annotated[ChatPickerQuery, Query()], principal: CurrentChatUser, session: SessionDep
) -> ChatPickerResponse:
    with tracer.start_as_current_span("coire.api.chat.picker") as span:
        span.set_attribute("chat.mode", query.mode)
        span.set_attribute("chat.action", query.action)
        try:
            response = await picker(session, principal)
        except Exception as exc:
            requests_total.add(1, {"operation": "picker", "outcome": "failed"})
            logger.error(
                "chat picker failed user_id=%s error_type=%s",
                principal.user_id,
                type(exc).__name__,
            )
            raise ChatModelUnavailable("chat service temporarily unavailable") from None
        requests_total.add(1, {"operation": "picker", "outcome": "succeeded"})
        logger.info("chat picker user_id=%s count=%d", principal.user_id, len(response.data))
        return response


@router.post("/conversations", response_model=ChatConversation, status_code=status.HTTP_201_CREATED)
async def create_chat_conversation(
    body: ChatConversationCreate, principal: CurrentChatUser, session: SessionDep
) -> ChatConversation:
    with tracer.start_as_current_span("coire.api.chat.create"):
        try:
            response = await create_conversation(session, principal, body)
        except CoireError:
            requests_total.add(1, {"operation": "create", "outcome": "refused"})
            raise
        except Exception as exc:
            requests_total.add(1, {"operation": "create", "outcome": "failed"})
            logger.error(
                "chat conversation creation failed user_id=%s error_type=%s",
                principal.user_id,
                type(exc).__name__,
            )
            raise ChatModelUnavailable("chat service temporarily unavailable") from None
        requests_total.add(1, {"operation": "create", "outcome": "succeeded"})
        logger.info(
            "chat conversation created user_id=%s conversation_id=%s",
            principal.user_id,
            response.id,
        )
        return response


@router.get("/conversations", response_model=ChatConversationPage)
async def list_chat_conversations(
    query: Annotated[ChatPageQuery, Query()],
    principal: CurrentChatUser,
    session: SessionDep,
) -> ChatConversationPage:
    with tracer.start_as_current_span("coire.api.chat.history_list"):
        try:
            page = await list_conversations(session, principal, query)
        except CoireError:
            requests_total.add(1, {"operation": "history_list", "outcome": "refused"})
            raise
        except Exception as exc:
            requests_total.add(1, {"operation": "history_list", "outcome": "failed"})
            logger.error(
                "chat history list failed user_id=%s error_type=%s",
                principal.user_id,
                type(exc).__name__,
            )
            raise ChatModelUnavailable("chat service temporarily unavailable") from None
        requests_total.add(1, {"operation": "history_list", "outcome": "succeeded"})
        return page


@router.get("/conversations/{conversation_id}", response_model=ChatConversationDetail)
async def get_chat_conversation(
    conversation_id: uuid.UUID,
    query: Annotated[ChatMessagePageQuery, Query()],
    principal: CurrentChatUser,
    session: SessionDep,
) -> ChatConversationDetail:
    with tracer.start_as_current_span("coire.api.chat.history_detail"):
        try:
            detail = await get_conversation_detail(session, principal, conversation_id, query)
        except CoireError:
            requests_total.add(1, {"operation": "history_detail", "outcome": "refused"})
            raise
        except Exception as exc:
            requests_total.add(1, {"operation": "history_detail", "outcome": "failed"})
            logger.error(
                "chat history detail failed user_id=%s conversation_id=%s error_type=%s",
                principal.user_id,
                conversation_id,
                type(exc).__name__,
            )
            raise ChatModelUnavailable("chat service temporarily unavailable") from None
        requests_total.add(1, {"operation": "history_detail", "outcome": "succeeded"})
        return detail


@router.get(
    "/conversations/{conversation_id}/events",
    response_model=ChatEvent,
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {"schema": {"$ref": "#/components/schemas/ChatEvent"}}}
        }
    },
)
async def get_chat_events(
    conversation_id: uuid.UUID,
    request: Request,
    principal: CurrentChatUser,
    session: SessionDep,
    settings: SettingsDep,
    last_event_id: Annotated[str | None, Header(max_length=128)] = None,
) -> StreamingResponse:
    from coire_api.auth import require_owned_chat

    with tracer.start_as_current_span("coire.api.chat.observe"):
        conversation = await require_owned_chat(session, conversation_id, principal)
        cursor: int | None = None
        if last_event_id is not None:
            parts = last_event_id.split(":")
            if len(parts) != 2 or parts[0] != str(conversation_id) or not parts[1].isdigit():
                raise ChatConflict("invalid conversation event cursor")
            cursor = int(parts[1])
            if cursor < 0 or cursor > conversation.event_cursor:
                raise ChatConflict("invalid conversation event cursor")
        requests_total.add(1, {"operation": "observe", "outcome": "accepted"})
        return StreamingResponse(
            observe_conversation(conversation_id, principal, request, settings, cursor),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        )


@router.post(
    "/conversations/{conversation_id}/turns",
    response_model=ChatEvent,
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {"schema": {"$ref": "#/components/schemas/ChatEvent"}}}
        }
    },
)
async def send_chat_turn(
    conversation_id: uuid.UUID,
    body: ChatTurnCreate,
    request: Request,
    principal: CurrentChatUser,
    session: SessionDep,
    settings: SettingsDep,
) -> StreamingResponse:
    with tracer.start_as_current_span("coire.api.chat.send"):
        try:
            admission = await admit_turn(session, conversation_id, principal, body, settings)
        except CoireError:
            requests_total.add(1, {"operation": "send", "outcome": "refused"})
            raise
        except Exception as exc:
            requests_total.add(1, {"operation": "send", "outcome": "failed"})
            logger.error(
                "chat turn admission failed user_id=%s error_type=%s",
                principal.user_id,
                type(exc).__name__,
            )
            raise ChatModelUnavailable("chat service temporarily unavailable") from None
        requests_total.add(1, {"operation": "send", "outcome": "accepted"})
        logger.info(
            "chat turn accepted user_id=%s conversation_id=%s turn_id=%s replay=%s",
            principal.user_id,
            conversation_id,
            admission.turn.id,
            admission.replay,
        )
        source = (
            replay_saved_events(admission, principal, request, settings)
            if admission.replay
            else native_stream(admission, principal, request, settings)
        )
        return StreamingResponse(
            source,
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        )


@router.get("/conversations/{conversation_id}/turns/{turn_id}", response_model=ChatTurnDetail)
async def get_chat_turn(
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
    principal: CurrentChatUser,
    session: SessionDep,
) -> ChatTurnDetail:
    with tracer.start_as_current_span("coire.api.chat.turn_status"):
        try:
            detail = await read_turn_detail(session, principal, conversation_id, turn_id)
        except CoireError:
            requests_total.add(1, {"operation": "turn_status", "outcome": "refused"})
            raise
        except Exception as exc:
            requests_total.add(1, {"operation": "turn_status", "outcome": "failed"})
            logger.error(
                "chat turn status failed user_id=%s turn_id=%s error_type=%s",
                principal.user_id,
                turn_id,
                type(exc).__name__,
            )
            raise ChatModelUnavailable("chat service temporarily unavailable") from None
        requests_total.add(1, {"operation": "turn_status", "outcome": "succeeded"})
        return detail


@router.post("/conversations/{conversation_id}/turns/{turn_id}/stop", response_model=ChatTurn)
async def stop_chat_turn(
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
    body: ChatStopRequest,
    principal: CurrentChatUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ChatTurn:
    with tracer.start_as_current_span("coire.api.chat.stop"):
        try:
            turn = await request_turn_stop(
                session, principal, conversation_id, turn_id, body, settings
            )
        except CoireError:
            requests_total.add(1, {"operation": "stop", "outcome": "refused"})
            raise
        except Exception as exc:
            requests_total.add(1, {"operation": "stop", "outcome": "failed"})
            logger.error(
                "chat stop failed user_id=%s turn_id=%s error_type=%s",
                principal.user_id,
                turn_id,
                type(exc).__name__,
            )
            raise ChatModelUnavailable("chat service temporarily unavailable") from None
        requests_total.add(1, {"operation": "stop", "outcome": "accepted"})
        return turn
