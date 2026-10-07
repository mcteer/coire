"""Model-free canonical text/tool serialization; tokenizer work belongs on Studios."""

from __future__ import annotations

import json
from typing import Any

from coire_core.errors import TrainingValidationError
from coire_core.models.conversation import Conversation, TextPart
from coire_core.models.gateway import ChatMessage


def openai_text_messages(conversation: Conversation) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for message in conversation.messages:
        if any(not isinstance(part, TextPart) for part in message.parts):
            raise TrainingValidationError("Text conversation rendering does not accept images")
        value: dict[str, Any] = {
            "role": message.role,
            "content": [part.model_dump(mode="json") for part in message.parts] or None,
        }
        if message.tool_call_id is not None:
            value["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            value["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            sort_keys=True,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        messages.append(ChatMessage.model_validate(value))
    return messages


def openai_tools(conversation: Conversation) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": tool.model_dump(mode="json")}
        for tool in conversation.tools
    ]
