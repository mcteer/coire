"""Anthropic Messages adaptation onto the OpenAI-shaped bare-engine endpoint."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from coire_core.models.gateway import AnthropicMessagesRequest


def to_openai_payload(body: AnthropicMessagesRequest, *, model_path: str) -> dict[str, object]:
    messages: list[dict[str, object]] = []
    if isinstance(body.system, str):
        messages.append({"role": "system", "content": body.system})
    elif body.system:
        text = "".join(str(block.get("text", "")) for block in body.system)
        messages.append({"role": "system", "content": text})
    for message in body.messages:
        content = message.content
        if isinstance(content, list):
            content = "".join(str(block.get("text", "")) for block in content)
        messages.append({"role": message.role, "content": content})
    payload: dict[str, object] = {
        "model": model_path,
        "messages": messages,
        "max_tokens": body.max_tokens,
        "stream": body.stream,
    }
    for name in ("temperature", "top_p", "tools", "tool_choice"):
        value = getattr(body, name)
        if value is not None:
            payload[name] = value
    if body.stop_sequences is not None:
        payload["stop"] = body.stop_sequences
    return payload


def from_openai_response(body: dict[str, object], *, model: uuid.UUID) -> dict[str, object]:
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    content = message.get("content", "") if isinstance(message, dict) else ""
    usage = body.get("usage", {})
    usage = usage if isinstance(usage, dict) else {}
    return {
        "id": str(body.get("id", f"msg_{uuid.uuid4().hex}")),
        "type": "message",
        "role": "assistant",
        "model": str(model),
        "content": [{"type": "text", "text": str(content or "")}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens", 0)),
            "output_tokens": int(usage.get("completion_tokens", 0)),
        },
    }


def _event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


async def from_openai_stream(
    source: AsyncIterator[bytes], *, model: uuid.UUID
) -> AsyncIterator[bytes]:
    message_id = f"msg_{uuid.uuid4().hex}"
    yield _event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": str(model),
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    yield _event(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )
    async for raw in source:
        for line in raw.decode().splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                chunk = json.loads(line[6:])
                text = chunk["choices"][0]["delta"].get("content")
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue
            if text:
                yield _event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
    yield _event("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield _event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 0},
        },
    )
    yield _event("message_stop", {"type": "message_stop"})
