"""Owner-scoped native chat requests, projections, and persisted events."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coire_core.models.files import ChatAttachment, ChatAttachmentSelection
from coire_core.models.registry import LoadState, Tag


class ChatPickerQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["chat", "code"] = "chat"
    action: Literal["chat", "research", "plan", "apply"] = "chat"


class ChatPickerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    display_name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    tags: list[Tag] = Field(default_factory=list, max_length=10)
    context_window: int | None = Field(default=None, ge=1)
    size_class: Literal["small", "medium", "large", "unknown"]
    load_state: LoadState
    estimated_warmup_seconds: float | None = Field(default=None, ge=0)
    verified: bool = False
    accepts_images: bool = False
    max_images: int | None = Field(default=None, ge=1, le=10)


class ChatPickerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[ChatPickerEntry] = Field(default_factory=list, max_length=1000)


class ChatConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=120)
    mode: Literal["chat", "code"] = "chat"
    model_id: uuid.UUID | None = None


class ChatConversationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    mode: Literal["chat", "code"] | None = None
    model_id: uuid.UUID | None = None


class ChatConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    owner_id: uuid.UUID
    title: str = Field(min_length=1, max_length=120)
    mode: Literal["chat", "code"] = "chat"
    selected_model_id: uuid.UUID | None = None
    revision: int = Field(ge=1)
    active_turn_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class ChatConversationPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[ChatConversation] = Field(default_factory=list, max_length=100)
    next_cursor: str | None = Field(default=None, max_length=256)


class ChatPageQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=50, ge=1, le=100)


class ChatMessagePageQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before_position: int | None = Field(default=None, ge=1)
    limit: int = Field(default=50, ge=1, le=100)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    conversation_id: uuid.UUID
    position: int = Field(ge=1)
    role: Literal["user", "assistant"]
    text: str = Field(default="", max_length=512 * 1024)
    reasoning: str = Field(default="", max_length=512 * 1024)
    model_id: uuid.UUID | None = None
    model_display_name: str | None = Field(default=None, max_length=120)
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    created_at: datetime


class ChatUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class ChatTurnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: uuid.UUID
    expected_revision: int = Field(ge=1)
    model_id: uuid.UUID
    content: str = Field(min_length=1, max_length=64 * 1024)
    attachments: list[ChatAttachmentSelection] = Field(default_factory=list, max_length=10)
    action: Literal["chat", "research", "plan", "apply"] = "chat"
    retry_of: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    source_revision: str | None = Field(default=None, max_length=128)
    plan_id: uuid.UUID | None = None
    research_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def unique_attachments(self) -> ChatTurnCreate:
        if len(self.content.encode("utf-8")) > 64 * 1024:
            raise ValueError("content exceeds 64 KiB")
        if len({selection.file_id for selection in self.attachments}) != len(self.attachments):
            raise ValueError("each attachment may be selected once")
        return self


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    conversation_id: uuid.UUID
    client_request_id: uuid.UUID
    accepted_revision: int = Field(ge=1)
    input_message_id: uuid.UUID
    assistant_message_id: uuid.UUID
    model_id: uuid.UUID
    model_display_name: str = Field(min_length=1, max_length=120)
    state: Literal[
        "accepted", "loading", "running", "completed", "failed", "stopped", "interrupted"
    ]
    action: Literal["chat", "research", "plan", "apply"] = "chat"
    usage: ChatUsage | None = None
    created_at: datetime
    updated_at: datetime


class ChatConversationDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation: ChatConversation
    messages: list[ChatMessage] = Field(default_factory=list, max_length=100)
    turns: list[ChatTurn] = Field(default_factory=list, max_length=100)
    attachments: list[ChatAttachment] = Field(default_factory=list, max_length=100)
    event_cursor: int = Field(ge=0)
    next_message_position: int | None = Field(default=None, ge=1)


class ChatTurnDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn: ChatTurn
    input_message: ChatMessage
    assistant_message: ChatMessage
    event_cursor: int = Field(ge=0)


class ChatTurnStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["turn.status"] = "turn.status"
    state: Literal["accepted", "queued", "loading", "running", "stop_requested"]
    estimate_seconds: float | None = Field(default=None, ge=0)
    queue_position: int | None = Field(default=None, ge=0)
    explanation: str | None = Field(default=None, max_length=500)


class ChatSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["snapshot"] = "snapshot"
    detail: ChatConversationDetail
    replacement: bool = False


class ChatConversationUpdated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["conversation.updated"] = "conversation.updated"
    conversation: ChatConversation
    reason: str = Field(max_length=100)


class ChatTurnAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["turn.accepted"] = "turn.accepted"
    turn: ChatTurn


class ChatMessageDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["message.delta"] = "message.delta"
    message_id: uuid.UUID
    channel: Literal["answer", "reasoning"]
    text: str = Field(min_length=1, max_length=64 * 1024)
    offset: int = Field(ge=1, le=512 * 1024)


class ChatTurnTerminal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["turn.terminal"] = "turn.terminal"
    state: Literal["completed", "failed", "stopped", "interrupted"]
    usage: ChatUsage | None = None
    answer_length: int = Field(ge=0, le=512 * 1024)
    reasoning_length: int = Field(ge=0, le=512 * 1024)
    safe_error: str | None = Field(default=None, max_length=500)


class ChatAttachmentChanged(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["attachment.changed"] = "attachment.changed"
    attachment: ChatAttachment | None = None
    removed_id: uuid.UUID | None = None


class ChatConversationDeleted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["conversation.deleted"] = "conversation.deleted"
    conversation_id: uuid.UUID
    revision: int = Field(ge=1)


ChatEventPayload = Annotated[
    ChatSnapshot
    | ChatConversationUpdated
    | ChatTurnAccepted
    | ChatTurnStatus
    | ChatMessageDelta
    | ChatTurnTerminal
    | ChatAttachmentChanged
    | ChatConversationDeleted,
    Field(discriminator="type"),
]


class ChatEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID
    cursor: int = Field(ge=1)
    turn_id: uuid.UUID | None = None
    created_at: datetime
    payload: ChatEventPayload

    @property
    def event_id(self) -> str:
        """Stable SSE ID, scoped to one conversation."""

        return f"{self.conversation_id}:{self.cursor}"


class ChatDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class ChatDeletionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    purge_deadline_at: datetime


class ChatStopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal["user_stop", "navigation"]
