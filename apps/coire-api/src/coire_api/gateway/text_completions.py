"""Translate canonical chat results without changing execution or accounting."""

import json
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any

from coire_core.models.gateway import TextCompletionResponse


def text_response(value: dict[str, Any], *, streaming: bool = False) -> TextCompletionResponse:
    choices = [
        {
            "index": choice["index"],
            "text": (choice.get("delta", {}) if streaming else choice["message"]).get("content")
            or "",
            "finish_reason": choice.get("finish_reason"),
        }
        for choice in value.get("choices", [])
    ]
    usage = value.get("usage")
    return TextCompletionResponse.model_validate(
        {
            "id": value["id"],
            "created": value["created"],
            "model": value["model"],
            "choices": choices,
            "usage": {
                key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            }
            if usage is not None
            else None,
        }
    )


def _frame(frame: bytes) -> bytes:
    lines = frame.splitlines()
    data = b"\n".join(line[5:].lstrip() for line in lines if line.startswith(b"data:"))
    # Keep load keepalives, RFC 9457 errors, and terminal framing intact.
    if not data or data == b"[DONE]":
        return frame + b"\n\n"
    value = json.loads(data)
    if "choices" not in value:
        return frame + b"\n\n"
    translated = text_response(value, streaming=True)
    return b"data: " + translated.model_dump_json(exclude_none=False).encode() + b"\n\n"


async def text_stream(source: AsyncGenerator[bytes]) -> AsyncGenerator[bytes]:
    """Handle frames split anywhere across network chunks; close the owned source."""
    pending = b""
    async with aclosing(source):
        async for chunk in source:
            pending += chunk
            while b"\n\n" in pending:
                frame, pending = pending.split(b"\n\n", 1)
                yield _frame(frame)
        if pending:
            yield _frame(pending)
