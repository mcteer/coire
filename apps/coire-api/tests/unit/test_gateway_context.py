import pytest

from coire_api.gateway.context import ContextLengthError, enforce_context
from coire_core.models.gateway import ChatMessage


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
