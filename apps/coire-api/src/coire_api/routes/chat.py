"""Owner-scoped native Chat API."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from coire_api.auth import CurrentChatUser
from coire_api.chat.service import create_conversation, picker
from coire_api.chat.telemetry import requests_total, tracer
from coire_api.deps import SessionDep
from coire_core.errors import CoireError
from coire_core.models.chat import (
    ChatConversation,
    ChatConversationCreate,
    ChatPickerQuery,
    ChatPickerResponse,
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
        except Exception:
            requests_total.add(1, {"operation": "picker", "outcome": "failed"})
            logger.exception("chat picker failed user_id=%s", principal.user_id)
            raise
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
        except Exception:
            requests_total.add(1, {"operation": "create", "outcome": "failed"})
            logger.exception("chat conversation creation failed user_id=%s", principal.user_id)
            raise
        requests_total.add(1, {"operation": "create", "outcome": "succeeded"})
        logger.info(
            "chat conversation created user_id=%s conversation_id=%s",
            principal.user_id,
            response.id,
        )
        return response
