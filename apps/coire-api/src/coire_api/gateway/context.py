"""Conservative preflight context checks without loading a tokenizer on core."""

from __future__ import annotations

from coire_core.models.gateway import AnthropicMessagesRequest, ChatMessage


class ContextLengthError(ValueError):
    def __init__(self, *, limit: int, estimated_tokens: int) -> None:
        self.limit = limit
        self.estimated_tokens = estimated_tokens
        super().__init__(f"estimated prompt size {estimated_tokens} exceeds context limit {limit}")


def estimate_chat_tokens(messages: list[ChatMessage]) -> int:
    """Overestimate common English/code prompts; engines remain the exact authority."""
    characters = sum(len(message.content or "") + len(message.role) + 8 for message in messages)
    return max(1, (characters + 2) // 3)


def enforce_context(messages: list[ChatMessage], *, limit: int | None, output_tokens: int) -> int:
    estimated = estimate_chat_tokens(messages)
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
