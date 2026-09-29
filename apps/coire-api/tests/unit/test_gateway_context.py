import base64
import io

import pytest
from PIL import Image

from coire_api.gateway.context import (
    ContextLengthError,
    VisualContextUnavailable,
    enforce_anthropic_context,
    enforce_context,
)
from coire_core.models.gateway import AnthropicMessagesRequest, ChatMessage
from coire_core.models.registry import VisualCapability


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


def test_text_parts_count_their_text_not_the_number_of_parts() -> None:
    message = ChatMessage.model_validate(
        {
            "role": "user",
            "content": [{"type": "text", "text": "x" * 90}],
        }
    )
    with pytest.raises(ContextLengthError):
        enforce_context([message], limit=10, output_tokens=4)


def test_inline_image_has_no_unmeasured_text_token_estimate() -> None:
    message = ChatMessage.model_validate(
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}],
        }
    )
    with pytest.raises(VisualContextUnavailable):
        enforce_context([message], limit=None, output_tokens=4)


def test_verified_inline_png_counts_visual_tokens_and_enforces_measured_limits() -> None:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), (255, 0, 0)).save(output, format="PNG")
    image = base64.b64encode(output.getvalue()).decode()
    message = ChatMessage.model_validate(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Name the color."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
            ],
        }
    )
    visual = VisualCapability(
        verified=True, max_images=1, max_image_pixels=256, max_encoded_bytes=len(output.getvalue())
    )
    assert enforce_context([message], limit=1000, output_tokens=32, visual=visual) >= 256
    with pytest.raises(VisualContextUnavailable, match="too many images"):
        enforce_context([message, message], limit=None, output_tokens=32, visual=visual)
    with pytest.raises(VisualContextUnavailable, match="byte limit"):
        enforce_context(
            [message],
            limit=None,
            output_tokens=32,
            visual=visual.model_copy(update={"max_encoded_bytes": len(output.getvalue()) - 1}),
        )
    with pytest.raises(VisualContextUnavailable, match="pixel limit"):
        enforce_context(
            [message],
            limit=None,
            output_tokens=32,
            visual=visual.model_copy(update={"max_image_pixels": 255}),
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
