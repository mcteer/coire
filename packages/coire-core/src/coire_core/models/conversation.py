"""Canonical ordered conversation content shared by native chat and harnesses."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

MAX_CONVERSATION_METADATA_BYTES = 64 * 1024
RESERVED_METADATA_KEYS = frozenset(
    {
        "verified",
        "authorization",
        "credentials",
        "runtime",
        "token",
        "entitlements",
        "scopes",
        "fence",
    }
)


def bounded_json_object(
    value: dict[str, JsonValue], *, metadata: bool = False
) -> dict[str, JsonValue]:
    """Keep inert imported JSON bounded; metadata cannot assert execution authority."""
    if metadata and any(
        key.lower().startswith("coire_") or key.lower() in RESERVED_METADATA_KEYS for key in value
    ):
        raise ValueError("metadata contains a reserved authority key")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, RecursionError) as error:
        raise ValueError("JSON object must be finite and bounded") from error
    if len(encoded.encode("utf-8")) > MAX_CONVERSATION_METADATA_BYTES:
        raise ValueError("JSON object exceeds its byte bound")
    pending: list[tuple[JsonValue, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 16:
            raise ValueError("JSON object exceeds its depth bound")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


class ToolCall(BaseModel):
    """Canonical inert tool invocation; argument strings are normalized at import."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    arguments: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return bounded_json_object(value)


class ConversationTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    description: str = Field(default="", max_length=4096)
    parameters: dict[str, JsonValue]

    @field_validator("parameters")
    @classmethod
    def bounded_schema(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return bounded_json_object(value)


class TextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=2_000_000)


class ImagePart(BaseModel):
    """A trusted asset reference, resolved to pixels only after authorization."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["image"] = "image"
    asset_id: uuid.UUID
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    width: int = Field(ge=1, le=2048)
    height: int = Field(ge=1, le=2048)

    @model_validator(mode="after")
    def bounded_pixels(self) -> ImagePart:
        if self.width * self.height > 4_000_000:
            raise ValueError("image exceeds four megapixels")
        return self


ConversationPart = Annotated[TextPart | ImagePart, Field(discriminator="type")]


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    role: Literal["system", "user", "assistant", "tool"]
    parts: list[ConversationPart] = Field(default_factory=list, max_length=64)
    created_at: datetime | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=32)
    tool_call_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return bounded_json_object(value, metadata=True)

    @model_validator(mode="after")
    def valid_content(self) -> ConversationMessage:
        if not self.parts and not (self.role == "assistant" and self.tool_calls):
            raise ValueError("a message needs content or assistant tool calls")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages may contain tool calls")
        if self.tool_call_id is not None and self.role != "tool":
            raise ValueError("only tool messages may reference a tool call")
        if len({call.id for call in self.tool_calls}) != len(self.tool_calls):
            raise ValueError("tool call IDs must be unique")
        return self


class Conversation(BaseModel):
    """Model-independent content with stable message identity and order."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    messages: list[ConversationMessage] = Field(default_factory=list, max_length=4096)
    tools: list[ConversationTool] = Field(default_factory=list, max_length=32)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return bounded_json_object(value, metadata=True)

    @model_validator(mode="after")
    def bounded_definitions(self) -> Conversation:
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("tool names must be unique")
        bounded_json_object(
            {
                "tools": [tool.model_dump(mode="json") for tool in self.tools],
                "metadata": self.metadata,
            }
        )
        return self
