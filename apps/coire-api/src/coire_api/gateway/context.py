"""Conservative preflight context checks without loading a tokenizer on core."""

from __future__ import annotations

from coire_core.models.gateway import ChatMessage


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
