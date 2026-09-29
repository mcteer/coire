"""Safe picker projection and owner-derived conversation creation."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import ChatConversationRow, EngineProcessRow, ModelRow
from coire_api.registry.service import chat_model_eligible, load_state_for
from coire_core.errors import ChatNotFound
from coire_core.models.chat import (
    ChatConversation,
    ChatConversationCreate,
    ChatPickerEntry,
    ChatPickerResponse,
)
from coire_core.models.registry import CapabilityProfile, EngineBackend, Tag, VisualCapability


def _size_class(memory_bytes: int) -> Literal["small", "medium", "large", "unknown"]:
    if memory_bytes <= 0:
        return "unknown"
    if memory_bytes <= 16 * 1024**3:
        return "small"
    if memory_bytes <= 64 * 1024**3:
        return "medium"
    return "large"


def _warmup(engines: Sequence[EngineProcessRow]) -> float | None:
    measured = [engine for engine in engines if engine.load_seconds is not None]
    if not measured:
        return None
    return max(measured, key=lambda engine: engine.started_at).load_seconds


async def picker(session: AsyncSession, principal: Principal) -> ChatPickerResponse:
    """Read published, ready, entitled models without any acquisition side effect."""
    rows = (
        (await session.execute(select(ModelRow).order_by(ModelRow.display_name, ModelRow.id)))
        .scalars()
        .all()
    )
    visible = [model for model in rows if chat_model_eligible(model, principal)]
    if not visible:
        return ChatPickerResponse()
    engines = (
        (
            await session.execute(
                select(EngineProcessRow).where(
                    EngineProcessRow.model_id.in_([model.id for model in visible])
                )
            )
        )
        .scalars()
        .all()
    )
    by_model: dict[uuid.UUID, list[EngineProcessRow]] = {}
    for engine in engines:
        if engine.model_id is not None:
            by_model.setdefault(engine.model_id, []).append(engine)
    entries: list[ChatPickerEntry] = []
    for model in visible:
        profile = CapabilityProfile.model_validate(model.capability_profile or {})
        visual = (
            VisualCapability.model_validate(model.visual_capability)
            if model.visual_capability is not None
            else None
        )
        accepts_images = (
            model.backend == EngineBackend.MLX_VLM and visual is not None and visual.verified
        )
        model_engines = by_model.get(model.id, [])
        entries.append(
            ChatPickerEntry(
                id=model.id,
                display_name=model.display_name,
                description=model.description,
                tags=[Tag(tag) for tag in model.tags or []],
                context_window=model.context_window,
                size_class=_size_class(model.memory_estimate_bytes),
                load_state=load_state_for(model_engines)[0],
                estimated_warmup_seconds=_warmup(model_engines),
                verified=profile.verified,
                accepts_images=accepts_images,
                max_images=visual.max_images if accepts_images and visual is not None else None,
            )
        )
    return ChatPickerResponse(data=entries)


def project_conversation(row: ChatConversationRow) -> ChatConversation:
    return ChatConversation(
        id=row.id,
        owner_id=row.owner_user_id,
        title=row.title,
        mode=cast(Literal["chat", "code"], row.mode),
        selected_model_id=row.selected_model_id,
        revision=row.revision,
        active_turn_id=row.active_turn_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def create_conversation(
    session: AsyncSession, principal: Principal, request: ChatConversationCreate
) -> ChatConversation:
    """Create a private draft. Owner, revision and identifiers never come from request JSON."""
    assert principal.user_id is not None
    if request.model_id is not None:
        model = await session.get(ModelRow, request.model_id)
        if model is None or not chat_model_eligible(model, principal):
            raise ChatNotFound()
    row = ChatConversationRow(
        id=uuid.uuid4(),
        owner_user_id=principal.user_id,
        title=request.title or "New conversation",
        mode=request.mode,
        selected_model_id=request.model_id,
        revision=1,
        event_cursor=0,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    await session.commit()
    return project_conversation(row)
