"""Native Chat feedback routes retain owner and browser-origin authorization."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from coire_api.auth import CurrentChatUser
from coire_api.deps import SessionDep
from coire_api.feedback import comparisons
from coire_api.feedback.service import (
    change_preference,
    change_thumb,
    conversation_feedback,
    read_preference,
)
from coire_api.feedback.telemetry import mutation_scope
from coire_api.routes.chat import require_chat_enabled
from coire_core.models.feedback import (
    ComparisonCreate,
    ComparisonDetail,
    ComparisonDismiss,
    ComparisonReceipt,
    ComparisonSelect,
    ComparisonSelectionReceipt,
    ConversationFeedbackPage,
    FeedbackPreference,
    FeedbackPreferenceUpdate,
    FeedbackReceipt,
    ThumbUpdate,
)
from coire_core.models.training_types import TrainingId
from coire_core.settings import Settings, get_settings


def feedback_settings(request: Request) -> Settings:
    configured = getattr(request.app.state, "settings", None)
    return configured if isinstance(configured, Settings) else get_settings()


FeedbackSettingsDep = Annotated[Settings, Depends(feedback_settings)]

router = APIRouter(
    prefix="/api/v1/chat", tags=["chat:feedback"], dependencies=[Depends(require_chat_enabled)]
)


@router.get("/feedback-preference", response_model=FeedbackPreference)
async def get_feedback_preference(
    principal: CurrentChatUser, session: SessionDep
) -> FeedbackPreference:
    result = await read_preference(session, principal)
    await session.commit()
    return result


@router.post(
    "/conversations/{conversation_id}/comparisons",
    status_code=202,
    response_model=ComparisonReceipt,
)
async def post_comparison(
    conversation_id: uuid.UUID,
    body: ComparisonCreate,
    principal: CurrentChatUser,
    settings: FeedbackSettingsDep,
) -> ComparisonReceipt:
    async with mutation_scope(principal, "comparison_create") as session:
        result = await comparisons.create_comparison(
            session, principal, conversation_id, body, settings
        )
        await session.commit()
    # Claiming queued state is transactional and duplicate dispatch cannot own
    # another generation. Authorised replay only returns the fresh receipt.
    if result.state == "queued":
        async with mutation_scope(principal, "comparison_dispatch") as session:
            _, pair = await comparisons.locked_pair(
                session, principal, conversation_id, result.id, live=True
            )
            if not pair.execution.get("dispatched"):
                execution = dict(pair.execution)
                execution["dispatched"] = True
                from coire_api.chat.maintenance import PROCESS_ID

                execution["owner_process"] = PROCESS_ID
                execution["lease_expires_at"] = (
                    datetime.now(UTC) + timedelta(seconds=10)
                ).isoformat()
                pair.execution = execution
                await session.commit()
                await comparisons.start_comparison(result.id, principal, settings)
    return result


@router.get(
    "/conversations/{conversation_id}/comparisons/{pair_id}", response_model=ComparisonDetail
)
async def get_comparison(
    conversation_id: uuid.UUID,
    pair_id: TrainingId,
    principal: CurrentChatUser,
    session: SessionDep,
) -> ComparisonDetail:
    result = await comparisons.comparison_detail(session, principal, conversation_id, pair_id)
    await session.commit()
    return result


@router.post(
    "/conversations/{conversation_id}/comparisons/{pair_id}/selection",
    response_model=ComparisonSelectionReceipt,
)
async def post_comparison_selection(
    conversation_id: uuid.UUID,
    pair_id: TrainingId,
    body: ComparisonSelect,
    principal: CurrentChatUser,
) -> ComparisonSelectionReceipt:
    async with mutation_scope(principal, "comparison_select") as session:
        result = await comparisons.select_comparison(
            session, principal, conversation_id, pair_id, body
        )
        await session.commit()
        return result


@router.post(
    "/conversations/{conversation_id}/comparisons/{pair_id}/dismiss",
    response_model=ComparisonReceipt,
)
async def post_comparison_dismiss(
    conversation_id: uuid.UUID,
    pair_id: TrainingId,
    body: ComparisonDismiss,
    principal: CurrentChatUser,
) -> ComparisonReceipt:
    async with mutation_scope(principal, "comparison_dismiss") as session:
        result = await comparisons.dismiss_comparison(
            session, principal, conversation_id, pair_id, body
        )
        await session.commit()
        return result


@router.patch("/feedback-preference", response_model=FeedbackPreference)
async def patch_feedback_preference(
    body: FeedbackPreferenceUpdate, principal: CurrentChatUser
) -> FeedbackPreference:
    async with mutation_scope(principal, "preference") as session:
        result = await change_preference(session, principal, body)
        await session.commit()
        return result


@router.put(
    "/conversations/{conversation_id}/messages/{message_id}/feedback",
    response_model=FeedbackReceipt,
)
async def put_message_feedback(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    body: ThumbUpdate,
    principal: CurrentChatUser,
) -> FeedbackReceipt:
    async with mutation_scope(principal, "thumb") as session:
        result = await change_thumb(session, principal, conversation_id, message_id, body)
        await session.commit()
        return result


@router.get("/conversations/{conversation_id}/feedback", response_model=ConversationFeedbackPage)
async def get_conversation_feedback(
    conversation_id: uuid.UUID,
    principal: CurrentChatUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=10, pattern=r"^[1-9][0-9]*$")] = None,
) -> ConversationFeedbackPage:
    result = await conversation_feedback(
        session,
        principal,
        conversation_id,
        limit=limit,
        after_position=int(cursor) if cursor is not None else None,
    )
    await session.commit()
    return result
