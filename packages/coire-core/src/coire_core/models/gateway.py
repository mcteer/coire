"""Strict wire contracts for the compatible inference gateway."""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from coire_core.models.registry import ModelSource


class OpenAITextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=2_000_000)


class OpenAIImageURL(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(max_length=14_000_000)
    detail: Literal["auto", "low", "high"] | None = None

    @field_validator("url")
    @classmethod
    def only_bounded_inline_images(cls, value: str) -> str:
        match = re.fullmatch(r"data:image/(?:png|jpeg|webp);base64,([A-Za-z0-9+/]+={0,2})", value)
        if match is None:
            raise ValueError("only inline PNG, JPEG, or WebP data images are allowed")
        encoded = match.group(1)
        if len(encoded) > 14_000_000:
            raise ValueError("inline image exceeds 10 MiB")
        try:
            decoded_bytes = len(base64.b64decode(encoded, validate=True))
        except (binascii.Error, ValueError) as exc:
            raise ValueError("inline image is not valid base64") from exc
        if decoded_bytes > 10 * 1024 * 1024:
            raise ValueError("inline image exceeds 10 MiB")
        return value


class OpenAIImagePart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["image_url"] = "image_url"
    image_url: OpenAIImageURL


OpenAIContentPart = Annotated[OpenAITextPart | OpenAIImagePart, Field(discriminator="type")]


class GatewayModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    object: Literal["model"] = "model"
    created: int
    owned_by: Literal["coire"] = "coire"
    coire_load_state: Literal["loaded", "loading", "cold"]
    coire_source: ModelSource = ModelSource.STUDIO
    coire_tags: list[str] = Field(default_factory=list)
    coire_description: str | None = None
    coire_context_window: int | None = Field(default=None, ge=1)


class GatewayModelList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object: Literal["list"] = "list"
    data: list[GatewayModel]


class ProblemDetails(BaseModel):
    """RFC 9457 problem detail used by the compatible gateway."""

    model_config = ConfigDict(extra="allow")

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    coire_code: str | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    # OpenAI assistant tool-call messages omit content entirely.  The gateway serialises null
    # fields away before crossing the node boundary, so the node contract must accept omission
    # as equivalent to an explicit null on this OpenAI-compatible shape.
    content: str | list[OpenAIContentPart] | None = None
    name: str | None = None
    tool_call_id: str | None = None

    @field_validator("content")
    @classmethod
    def bounded_parts(
        cls, value: str | list[OpenAIContentPart] | None
    ) -> str | list[OpenAIContentPart] | None:
        if isinstance(value, list) and (not value or len(value) > 64):
            raise ValueError("content parts must contain 1..64 items")
        if (
            isinstance(value, list)
            and sum(isinstance(part, OpenAIImagePart) for part in value) > 10
        ):
            raise ValueError("content exceeds ten images")
        return value


class EngineStreamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_usage: bool


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: uuid.UUID
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: EngineStreamOptions | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0)
    top_p: float | None = Field(default=None, ge=0, le=1)
    stop: str | list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    response_format: dict[str, Any] | None = None
    coire_wait_for_model: bool = True
    coire_affinity_node: str | None = Field(default=None, pattern=r"^coire-[a-z0-9-]+$")


class AnthropicMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]


class AnthropicMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: uuid.UUID
    max_tokens: int = Field(ge=1)
    messages: list[AnthropicMessage] = Field(min_length=1)
    system: str | list[dict[str, Any]] | None = None
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=0)
    stop_sequences: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | None = None
    output_config: dict[str, Any] | None = None
    thinking: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    service_tier: str | None = None
    context_management: dict[str, Any] | None = None
    container: str | dict[str, Any] | None = None
    mcp_servers: list[dict[str, Any]] | None = None
    coire_wait_for_model: bool = True
    coire_affinity_node: str | None = Field(default=None, pattern=r"^coire-[a-z0-9-]+$")


class EngineChatRequest(BaseModel):
    """Registry-resolved payload carried from the gateway to a node-owned engine."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=4096)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: EngineStreamOptions | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0)
    top_p: float | None = Field(default=None, ge=0, le=1)
    stop: str | list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    response_format: dict[str, Any] | None = None


class GatewayProtocol(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class UsageOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DISCONNECTED = "disconnected"
    STOPPED = "stopped"
    REFUSED = "refused"


class UsageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    request_id: uuid.UUID
    principal_kind: str
    principal_subject: str | None
    requested_model_id: str
    model_id: uuid.UUID | None
    engine_id: uuid.UUID | None
    protocol: GatewayProtocol
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    duration_ms: float = Field(ge=0)
    outcome: UsageOutcome
    failure_code: str | None
    started_at: datetime
    finished_at: datetime
