"""Capability-selected parsing with one normalized tool-call shape."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from coire_core.models.harness import HarnessStrategy, ToolCall

_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_THINK = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


class ToolParseError(ValueError):
    pass


def strip_reasoning(text: str) -> tuple[str, str | None]:
    blocks = [part.strip() for part in _THINK.findall(text) if part.strip()]
    return _THINK.sub("", text).strip(), "\n\n".join(blocks) or None


def parse_tool_call(strategy: HarnessStrategy, payload: object) -> ToolCall:
    try:
        if strategy is HarnessStrategy.NATIVE:
            return ToolCall.model_validate(payload)
        if not isinstance(payload, str):
            raise ToolParseError("text strategy requires a string response")
        clean, _ = strip_reasoning(payload)
        if strategy is HarnessStrategy.DELIMITED:
            match = _CALL.search(clean)
            if match is None:
                raise ToolParseError("missing <tool_call> delimiter")
            clean = match.group(1)
        parsed: Any = json.loads(clean)
        return ToolCall.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ToolParseError(f"invalid tool call: {exc}") from exc
