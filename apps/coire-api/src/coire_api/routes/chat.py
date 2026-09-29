"""Owner-scoped native Chat API."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from coire_api.auth import CurrentChatUser
from coire_api.chat.service import create_conversation, picker
from coire_api.chat.streaming import native_stream, replay_saved_events
from coire_api.chat.telemetry import requests_total, tracer
from coire_api.chat.turns import admit_turn, read_turn_detail
from coire_api.deps import SessionDep, SettingsDep
from coire_core.errors import ChatModelUnavailable, CoireError
from coire_core.models.chat import (
    ChatConversation,
    ChatConversationCreate,
    ChatPickerQuery,
    ChatPickerResponse,
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


@router.post("/conversations/{conversation_id}/turns")
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
