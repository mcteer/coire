import pytest

from coire_api.gateway.context import ContextLengthError, enforce_anthropic_context, enforce_context
from coire_core.models.gateway import AnthropicMessagesRequest, ChatMessage


def test_context_error_names_estimate_and_limit() -> None:
    messages = [ChatMessage(role="user", content="x" * 90)]
    with pytest.raises(ContextLengthError, match="exceeds context limit 10") as caught:
        enforce_context(messages, limit=10, output_tokens=4)
    assert caught.value.limit == 10
    assert caught.value.estimated_tokens > 10


def test_unknown_context_window_does_not_refuse() -> None:
    assert (
        enforce_context([ChatMessage(role="user", content="hello")], limit=None, output_tokens=100)
        > 0
    )


def test_anthropic_context_limit_includes_system_blocks_and_output() -> None:
    body = AnthropicMessagesRequest.model_validate(
        {
            "model": "00000000-0000-0000-0000-000000000001",
            "max_tokens": 8,
            "system": [{"type": "text", "text": "system instruction"}],
            "messages": [{"role": "user", "content": "hello world"}],
        }
    )
    with pytest.raises(ContextLengthError) as caught:
        enforce_anthropic_context(body, limit=10)
    assert caught.value.limit == 10
