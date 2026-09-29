"""Owner-scoped native Chat API."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response, StreamingResponse
from pydantic import ValidationError

from coire_api.auth import CurrentChatUser, require_owned_chat
from coire_api.chat.files import (
    admit_original,
    owned_attachment,
    project_attachment,
    read_original,
    render_pdf_pages,
    retry_inspection,
    stage_original,
)
from coire_api.chat.processing import InvalidAsset, read_private_preview
from coire_api.chat.service import (
    create_conversation,
    delete_conversation,
    get_conversation_detail,
    list_conversations,
    picker,
    update_conversation,
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
    ChatConversationUpdate,
    ChatDeleteRequest,
    ChatDeletionResult,
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
from coire_core.models.files import ChatAttachment, ChatFileProcessRequest, ChatUploadMetadata

logger = logging.getLogger(__name__)


def require_chat_enabled(request: Request) -> None:
    if not request.app.state.settings.chat_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


router = APIRouter(
    prefix="/api/v1/chat", tags=["chat"], dependencies=[Depends(require_chat_enabled)]
)


@router.post(
    "/conversations/{conversation_id}/files",
    response_model=ChatAttachment,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_chat_file(
    conversation_id: uuid.UUID,
    filename: Annotated[str, Form(max_length=255)],
    expected_revision: Annotated[int, Form(ge=1)],
    file: Annotated[UploadFile, File()],
    principal: CurrentChatUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ChatAttachment:
    try:
        parsed_metadata = ChatUploadMetadata(filename=filename, expected_revision=expected_revision)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid upload metadata"
        ) from exc
    with tracer.start_as_current_span("coire.api.chat.upload") as span:
        span.set_attribute("user_id", str(principal.user_id))
        span.set_attribute("conversation_id", str(conversation_id))
        try:
            await require_owned_chat(session, conversation_id, principal)
            staged = await stage_original(
                file, Path(settings.chat_original_root), settings.chat_upload_max_bytes
            )
            result = await admit_original(
                session, principal, conversation_id, parsed_metadata, staged, settings
            )
        except CoireError:
            requests_total.add(1, {"operation": "upload", "outcome": "refused"})
            raise
        except Exception as exc:
            requests_total.add(1, {"operation": "upload", "outcome": "failed"})
            logger.error(
                "chat upload failed user_id=%s conversation_id=%s error_type=%s",
                principal.user_id,
                conversation_id,
                type(exc).__name__,
            )
            raise ChatModelUnavailable("chat file service temporarily unavailable") from None
        requests_total.add(1, {"operation": "upload", "outcome": "accepted"})
        logger.info(
            "chat upload accepted user_id=%s conversation_id=%s file_id=%s",
            principal.user_id,
            conversation_id,
            result.id,
        )
        return result


@router.get("/conversations/{conversation_id}/files/{file_id}", response_model=ChatAttachment)
async def get_chat_file(
    conversation_id: uuid.UUID,
    file_id: uuid.UUID,
    principal: CurrentChatUser,
    session: SessionDep,
) -> ChatAttachment:
    return project_attachment(await owned_attachment(session, principal, conversation_id, file_id))


@router.post(
    "/conversations/{conversation_id}/files/{file_id}/process",
    response_model=ChatAttachment,
    status_code=status.HTTP_202_ACCEPTED,
)
async def process_chat_file(
    conversation_id: uuid.UUID,
    file_id: uuid.UUID,
    body: ChatFileProcessRequest,
    principal: CurrentChatUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ChatAttachment:
    operation = "file_render" if body.operation == "render" else "file_retry"
    with tracer.start_as_current_span(f"coire.api.chat.{operation}") as span:
        span.set_attribute("file_id", str(file_id))
        try:
            action = render_pdf_pages if body.operation == "render" else retry_inspection
            result = await action(session, principal, conversation_id, file_id, body, settings)
        except CoireError:
            requests_total.add(1, {"operation": operation, "outcome": "refused"})
            raise
        except Exception as exc:
            requests_total.add(1, {"operation": operation, "outcome": "failed"})
            logger.error(
                "chat file retry failed user_id=%s file_id=%s error_type=%s",
                principal.user_id,
                file_id,
                type(exc).__name__,
            )
            raise ChatModelUnavailable("chat file service temporarily unavailable") from None
        requests_total.add(1, {"operation": operation, "outcome": "accepted"})
        return result


@router.get(
    "/conversations/{conversation_id}/files/{file_id}/content",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
async def download_chat_file(
    conversation_id: uuid.UUID,
    file_id: uuid.UUID,
    principal: CurrentChatUser,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    attachment = await owned_attachment(session, principal, conversation_id, file_id)
    data = await asyncio.to_thread(read_original, attachment, Path(settings.chat_original_root))
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(attachment.filename, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "sandbox",
            "Cross-Origin-Resource-Policy": "same-origin",
        },
    )


@router.get(
    "/conversations/{conversation_id}/files/{file_id}/previews/{asset_id}",
    response_class=Response,
    responses={200: {"content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}},
)
async def preview_chat_file(
    conversation_id: uuid.UUID,
    file_id: uuid.UUID,
    asset_id: uuid.UUID,
    principal: CurrentChatUser,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    with tracer.start_as_current_span("coire.api.chat.preview") as span:
        span.set_attribute("file_id", str(file_id))
        attachment = await owned_attachment(session, principal, conversation_id, file_id)
        try:
            data = await asyncio.to_thread(
                read_private_preview, attachment, asset_id, Path(settings.chat_derived_root)
            )
        except InvalidAsset:
            requests_total.add(1, {"operation": "preview", "outcome": "refused"})
            raise ChatConflict("file preview unavailable") from None
        requests_total.add(1, {"operation": "preview", "outcome": "served"})
        return Response(
            content=data,
            media_type="image/png",
            headers={
                "Content-Disposition": "inline",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "Content-Security-Policy": "sandbox",
                "Cross-Origin-Resource-Policy": "same-origin",
            },
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


@router.patch("/conversations/{conversation_id}", response_model=ChatConversation)
async def patch_chat_conversation(
    conversation_id: uuid.UUID,
    body: ChatConversationUpdate,
    principal: CurrentChatUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ChatConversation:
    with tracer.start_as_current_span("coire.api.chat.update"):
        try:
            updated = await update_conversation(session, principal, conversation_id, body, settings)
        except CoireError:
            requests_total.add(1, {"operation": "update", "outcome": "refused"})
            raise
        except Exception as exc:
            requests_total.add(1, {"operation": "update", "outcome": "failed"})
            logger.error(
                "chat update failed user_id=%s conversation_id=%s error_type=%s",
                principal.user_id,
                conversation_id,
                type(exc).__name__,
            )
            raise ChatModelUnavailable("chat service temporarily unavailable") from None
        requests_total.add(1, {"operation": "update", "outcome": "succeeded"})
        return updated


@router.delete(
    "/conversations/{conversation_id}",
    response_model=ChatDeletionResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_chat_conversation(
    conversation_id: uuid.UUID,
    body: ChatDeleteRequest,
    principal: CurrentChatUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ChatDeletionResult:
    with tracer.start_as_current_span("coire.api.chat.delete"):
        try:
            result = await delete_conversation(session, principal, conversation_id, body, settings)
        except CoireError:
            requests_total.add(1, {"operation": "delete", "outcome": "refused"})
            raise
        except Exception as exc:
            requests_total.add(1, {"operation": "delete", "outcome": "failed"})
            logger.error(
                "chat deletion failed user_id=%s conversation_id=%s error_type=%s",
                principal.user_id,
                conversation_id,
                type(exc).__name__,
            )
            raise ChatModelUnavailable("chat service temporarily unavailable") from None
        requests_total.add(1, {"operation": "delete", "outcome": "accepted"})
        return result


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
