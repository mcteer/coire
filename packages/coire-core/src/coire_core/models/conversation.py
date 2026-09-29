"""Canonical ordered conversation content shared by native chat and harnesses."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    parts: list[ConversationPart] = Field(min_length=1, max_length=64)
    created_at: datetime | None = None


class Conversation(BaseModel):
    """Model-independent content with stable message identity and order."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    messages: list[ConversationMessage] = Field(default_factory=list, max_length=4096)
