"""Strict wire contracts for the compatible inference gateway."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class GatewayModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    object: Literal["model"] = "model"
    created: int
    owned_by: Literal["coire"] = "coire"
    coire_load_state: Literal["loaded", "loading", "cold"]
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


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None
    name: str | None = None
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: uuid.UUID
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0)
    top_p: float | None = Field(default=None, ge=0, le=1)
    stop: str | list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    response_format: dict[str, Any] | None = None
    coire_wait_for_model: bool = True


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


class EngineChatRequest(BaseModel):
    """Registry-resolved payload carried from the gateway to a node-owned engine."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=4096)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
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
