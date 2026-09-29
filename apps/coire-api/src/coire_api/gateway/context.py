"""Conservative preflight context checks without loading a tokenizer on core."""

from __future__ import annotations

import base64

from coire_core.models.gateway import (
    AnthropicMessagesRequest,
    ChatMessage,
    OpenAIImagePart,
    OpenAITextPart,
)
from coire_core.models.registry import VisualCapability


class ContextLengthError(ValueError):
    def __init__(self, *, limit: int, estimated_tokens: int) -> None:
        self.limit = limit
        self.estimated_tokens = estimated_tokens
        super().__init__(f"estimated prompt size {estimated_tokens} exceeds context limit {limit}")


class VisualContextUnavailable(ValueError):
    """Inline image admission awaits bounded temporary visual processing."""


def _inline_image_tokens(part: OpenAIImagePart, visual: VisualCapability) -> int:
    """Bound a data PNG by the variant's measured visual input limits before engine I/O."""
    url = part.image_url.url
    if not url.startswith("data:image/png;base64,"):
        raise VisualContextUnavailable("this visual model accepts inline PNG only")
    data = base64.b64decode(url.partition(",")[2], validate=True)
    if len(data) > visual.max_encoded_bytes:
        raise VisualContextUnavailable("inline image exceeds this model's measured byte limit")
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise VisualContextUnavailable("inline image is not a valid PNG header")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    pixels = width * height
    if not width or not height or pixels > visual.max_image_pixels:
        raise VisualContextUnavailable("inline image exceeds this model's measured pixel limit")
    return max(256, (pixels + 63) // 64)


def estimate_chat_tokens(
    messages: list[ChatMessage], *, visual: VisualCapability | None = None
) -> int:
    """Overestimate common English/code prompts; engines remain the exact authority."""
    characters = 0
    image_tokens = 0
    image_count = 0
    for message in messages:
        content = message.content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, OpenAIImagePart):
                    if visual is None or not visual.verified:
                        raise VisualContextUnavailable(
                            "selected model has no verified visual input"
                        )
                    image_count += 1
                    if image_count > visual.max_images:
                        raise VisualContextUnavailable("too many images for the selected model")
                    image_tokens += _inline_image_tokens(part, visual)
            characters += sum(
                len(part.text) for part in content if isinstance(part, OpenAITextPart)
            )
        elif isinstance(content, str):
            characters += len(content)
        characters += len(message.role) + 8
    return max(1, (characters + 2) // 3 + image_tokens)


def enforce_context(
    messages: list[ChatMessage],
    *,
    limit: int | None,
    output_tokens: int,
    visual: VisualCapability | None = None,
) -> int:
    estimated = estimate_chat_tokens(messages, visual=visual)
    if limit is not None and estimated + output_tokens > limit:
        raise ContextLengthError(limit=limit, estimated_tokens=estimated + output_tokens)
    return estimated


def enforce_anthropic_context(body: AnthropicMessagesRequest, *, limit: int | None) -> int:
    """Conservatively count text in Anthropic strings and content blocks."""

    def characters(value: object) -> int:
        if isinstance(value, str):
            return len(value)
        if isinstance(value, list):
            return sum(characters(item) for item in value)
        if isinstance(value, dict):
            return sum(characters(item) for item in value.values())
        return 0

    total = characters(body.system) + sum(
        characters(message.content) + len(message.role) + 8 for message in body.messages
    )
    estimated = max(1, (total + 2) // 3)
    requested = estimated + body.max_tokens
    if limit is not None and requested > limit:
        raise ContextLengthError(limit=limit, estimated_tokens=requested)
    return estimated
