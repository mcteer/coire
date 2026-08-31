import uuid
from collections.abc import AsyncIterator
from typing import cast

import pytest

from coire_api.gateway.anthropic import from_openai_stream, to_openai_payload
from coire_core.models.gateway import AnthropicMessagesRequest


def test_translation_preserves_system_and_turn_order() -> None:
    body = AnthropicMessagesRequest.model_validate(
        {
            "model": str(uuid.uuid4()),
            "max_tokens": 20,
            "system": "rules",
            "messages": [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
            ],
        }
    )
    payload = to_openai_payload(body, model_path="/resolved/model")
    assert payload["model"] == "/resolved/model"
    messages = cast(list[dict[str, object]], payload["messages"])
    assert [item["role"] for item in messages] == ["system", "user", "assistant", "user"]


@pytest.mark.asyncio
async def test_stream_emits_anthropic_event_sequence() -> None:
    async def source() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    output = b"".join([event async for event in from_openai_stream(source(), model=uuid.uuid4())])
    names = [line for line in output.decode().splitlines() if line.startswith("event:")]
    assert names == [
        "event: message_start",
        "event: content_block_start",
        "event: content_block_delta",
        "event: content_block_stop",
        "event: message_delta",
        "event: message_stop",
    ]
