"""Safe picker projection and owner-derived conversation creation."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    ChatAttachmentRow,
    ChatConversationRow,
    ChatEventRow,
    ChatMessageRow,
    ChatTurnRow,
    EngineProcessRow,
    McpArtifactRow,
    ModelRow,
    ModelVariantRow,
)
from coire_api.registry.service import chat_model_eligible, load_state_for
from coire_api.runs import variant_gate
from coire_core.errors import ChatConflict, ChatNotFound
from coire_core.models.chat import (
    ChatConversation,
    ChatConversationCreate,
    ChatConversationDeleted,
    ChatConversationDetail,
    ChatConversationPage,
    ChatConversationUpdate,
    ChatConversationUpdated,
    ChatDeleteRequest,
    ChatDeletionResult,
    ChatEvent,
    ChatMessagePageQuery,
    ChatPageQuery,
    ChatPickerEntry,
    ChatPickerQuery,
    ChatPickerResponse,
)
from coire_core.models.harness import PROFILE_MODEL_TAGS, ProfileName, TaskClass
from coire_core.models.registry import (
    CapabilityProfile,
    EngineBackend,
    LoadState,
    ModelSource,
    Tag,
    VisualCapability,
)
from coire_core.settings import Settings


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


async def picker(
    session: AsyncSession,
    principal: Principal,
    query: ChatPickerQuery | None = None,
    settings: Settings | None = None,
) -> ChatPickerResponse:
    """Read published, ready, entitled models without any acquisition side effect."""
    rows = (
        (await session.execute(select(ModelRow).order_by(ModelRow.display_name, ModelRow.id)))
        .scalars()
        .all()
    )
    visible = [model for model in rows if chat_model_eligible(model, principal)]
    if settings is not None:
        from coire_api.gateway.providers import credential_present

        visible = [
            model
            for model in visible
            if (model.source or "studio") == "studio"
            or (
                settings.provider_chat_enabled
                and credential_present(ModelSource(model.source), settings)
            )
        ]
    query = query or ChatPickerQuery()
    if query.mode == "code":
        task_class = TaskClass.WRITE if query.action == "apply" else TaskClass.READ
        visible = [
            model
            for model in visible
            if (model.source or "studio") == ModelSource.STUDIO
            and set(model.tags or []).intersection(PROFILE_MODEL_TAGS[ProfileName.CODING])
        ]
        if visible:
            eligible_ids = set(
                (
                    await session.scalars(
                        select(ModelVariantRow.model_id).where(
                            ModelVariantRow.model_id.in_([model.id for model in visible]),
                            *variant_gate(task_class),
                        )
                    )
                ).all()
            )
            visible = [model for model in visible if model.id in eligible_ids]
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
        source = ModelSource(model.source or "studio")
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
                source=source,
                display_name=model.display_name,
                description=model.description,
                tags=[Tag(tag) for tag in model.tags or []],
                context_window=model.context_window,
                size_class=_size_class(model.memory_estimate_bytes),
                load_state=(
                    load_state_for(model_engines)[0]
                    if source is ModelSource.STUDIO
                    else LoadState.LOADED
                ),
                estimated_warmup_seconds=(
                    _warmup(model_engines) if source is ModelSource.STUDIO else None
                ),
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


def _encode_cursor(row: ChatConversationRow) -> str:
    payload = json.dumps([row.updated_at.isoformat(), str(row.id)], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        parsed = json.loads(raw)
        if (
            not isinstance(parsed, list)
            or len(parsed) != 2
            or not all(isinstance(part, str) for part in parsed)
        ):
            raise ValueError("invalid cursor")
        timestamp = datetime.fromisoformat(parsed[0])
        if timestamp.tzinfo is None:
            raise ValueError("cursor needs timezone")
        return timestamp, uuid.UUID(parsed[1])
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ChatConflict("invalid conversation cursor") from exc


async def list_conversations(
    session: AsyncSession, principal: Principal, query: ChatPageQuery
) -> ChatConversationPage:
    assert principal.user_id is not None
    statement = select(ChatConversationRow).where(
        ChatConversationRow.owner_user_id == principal.user_id,
        ChatConversationRow.deleted_at.is_(None),
    )
    if query.cursor is not None:
        timestamp, row_id = _decode_cursor(query.cursor)
        statement = statement.where(
            or_(
                ChatConversationRow.updated_at < timestamp,
                and_(
                    ChatConversationRow.updated_at == timestamp,
                    ChatConversationRow.id < row_id,
                ),
            )
        )
    rows = (
        (
            await session.execute(
                statement.order_by(
                    ChatConversationRow.updated_at.desc(), ChatConversationRow.id.desc()
                ).limit(query.limit + 1)
            )
        )
        .scalars()
        .all()
    )
    page = rows[: query.limit]
    return ChatConversationPage(
        data=[project_conversation(row) for row in page],
        next_cursor=_encode_cursor(page[-1]) if len(rows) > query.limit else None,
    )


async def get_conversation_detail(
    session: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    query: ChatMessagePageQuery,
) -> ChatConversationDetail:
    assert principal.user_id is not None
    row = await session.scalar(
        select(ChatConversationRow)
        .where(
            ChatConversationRow.id == conversation_id,
            ChatConversationRow.owner_user_id == principal.user_id,
            ChatConversationRow.deleted_at.is_(None),
        )
        .with_for_update(read=True)
    )
    if row is None:
        raise ChatNotFound()
    statement = select(ChatMessageRow).where(ChatMessageRow.conversation_id == conversation_id)
    if query.before_position is not None:
        statement = statement.where(ChatMessageRow.position < query.before_position)
    newest = (
        (
            await session.execute(
                statement.order_by(ChatMessageRow.position.desc()).limit(query.limit + 1)
            )
        )
        .scalars()
        .all()
    )
    page = list(reversed(newest[: query.limit]))
    answer_ids = [message.id for message in page if message.role == "assistant"]
    turns: list[ChatTurnRow] = []
    if answer_ids:
        turns = list(
            (
                await session.execute(
                    select(ChatTurnRow)
                    .where(
                        ChatTurnRow.conversation_id == conversation_id,
                        ChatTurnRow.assistant_message_id.in_(answer_ids),
                    )
                    .order_by(ChatTurnRow.created_at)
                )
            )
            .scalars()
            .all()
        )
    attachments = list(
        (
            await session.execute(
                select(ChatAttachmentRow)
                .where(
                    ChatAttachmentRow.conversation_id == conversation_id,
                    ChatAttachmentRow.owner_user_id == principal.user_id,
                    ChatAttachmentRow.deleted_at.is_(None),
                )
                .order_by(ChatAttachmentRow.created_at, ChatAttachmentRow.id)
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    from coire_api.chat.files import project_attachment
    from coire_api.chat.turns import project_message, project_turn

    return ChatConversationDetail(
        conversation=project_conversation(row),
        messages=[project_message(message) for message in page],
        turns=[project_turn(turn) for turn in turns],
        attachments=[project_attachment(attachment) for attachment in attachments],
        event_cursor=row.event_cursor,
        next_message_position=page[0].position if len(newest) > query.limit and page else None,
    )


async def update_conversation(
    session: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    body: ChatConversationUpdate,
    settings: Settings,
) -> ChatConversation:
    """Apply a versioned owner edit and commit its observer event atomically."""
    row = await session.scalar(
        select(ChatConversationRow)
        .where(
            ChatConversationRow.id == conversation_id,
            ChatConversationRow.owner_user_id == principal.user_id,
            ChatConversationRow.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        raise ChatNotFound()
    if row.revision != body.expected_revision:
        raise ChatConflict("conversation changed; reload before editing")
    if body.mode is not None and body.mode != row.mode:
        raise ChatConflict("code mode is not available yet")
    changed = False
    if body.model_id is not None and body.model_id != row.selected_model_id:
        if row.active_turn_id is not None:
            raise ChatConflict("cannot change model during an active turn")
        model = await session.get(ModelRow, body.model_id)
        if model is None or not chat_model_eligible(model, principal):
            raise ChatNotFound()
        row.selected_model_id = model.id
        changed = True
    if body.title is not None and body.title != row.title:
        row.title = body.title
        changed = True
    if not changed:
        return project_conversation(row)
    now = datetime.now(UTC)
    row.revision += 1
    row.event_cursor += 1
    row.updated_at = now
    event = ChatEvent(
        conversation_id=row.id,
        cursor=row.event_cursor,
        created_at=now,
        payload=ChatConversationUpdated(conversation=project_conversation(row), reason="edit"),
    )
    session.add(
        ChatEventRow(
            id=uuid.uuid4(),
            conversation_id=row.id,
            cursor=event.cursor,
            type=event.payload.type,
            payload=event.payload.model_dump(mode="json"),
            created_at=now,
            expires_at=now + timedelta(hours=settings.chat_event_retention_hours),
        )
    )
    await session.commit()
    return project_conversation(row)


async def delete_conversation(
    session: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    body: ChatDeleteRequest,
    settings: Settings,
) -> ChatDeletionResult:
    """Tombstone immediately; later maintenance purges all associated content."""
    row = await session.scalar(
        select(ChatConversationRow)
        .where(
            ChatConversationRow.id == conversation_id,
            ChatConversationRow.owner_user_id == principal.user_id,
        )
        .with_for_update()
    )
    if row is None:
        raise ChatNotFound()
    if row.deleted_at is not None:
        return ChatDeletionResult(
            id=row.id,
            purge_deadline_at=row.deleted_at + timedelta(hours=settings.chat_purge_deadline_hours),
        )
    if row.revision != body.expected_revision:
        raise ChatConflict("conversation changed; reload before deleting")
    if row.active_turn_id is not None:
        turn = await session.get(ChatTurnRow, row.active_turn_id)
        if turn is not None and turn.state not in {"completed", "failed", "stopped", "interrupted"}:
            if turn.action != "chat":
                raise ChatConflict("code run deletion requires the coding kill path")
            turn.state = "stop_requested"
            turn.stop_reason = "conversation_deleted"
            turn.updated_at = datetime.now(UTC)
    now = datetime.now(UTC)
    artifacts = (
        (
            await session.execute(
                select(McpArtifactRow)
                .join(ChatTurnRow, ChatTurnRow.coding_call_id == McpArtifactRow.call_id)
                .where(ChatTurnRow.conversation_id == row.id)
            )
        )
        .scalars()
        .all()
    )
    for artifact in artifacts:
        artifact.expires_at = now
    row.deleted_at = now
    row.updated_at = now
    row.revision += 1
    row.event_cursor += 1
    event = ChatEvent(
        conversation_id=row.id,
        cursor=row.event_cursor,
        created_at=now,
        payload=ChatConversationDeleted(conversation_id=row.id, revision=row.revision),
    )
    session.add(
        ChatEventRow(
            id=uuid.uuid4(),
            conversation_id=row.id,
            cursor=event.cursor,
            type=event.payload.type,
            payload=event.payload.model_dump(mode="json"),
            created_at=now,
            expires_at=now + timedelta(hours=settings.chat_event_retention_hours),
        )
    )
    await session.commit()
    return ChatDeletionResult(
        id=row.id,
        purge_deadline_at=now + timedelta(hours=settings.chat_purge_deadline_hours),
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
