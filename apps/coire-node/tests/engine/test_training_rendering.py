"""Compare canonical SFT input with pinned bare-serving tokenizer primitives."""

import copy
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from coire_core.conversation_rendering import openai_text_messages, openai_tools
from coire_core.models.conversation import (
    Conversation,
    ConversationMessage,
    ConversationTool,
    TextPart,
    ToolCall,
)
from coire_core.models.datasets import TrainingExample
from coire_node.training.rendering import ChatTokenizer, render_example

pytestmark = pytest.mark.engine


def test_completed_turns_match_serving_and_generation_suffix_is_explicit(
    training_model: Path,
) -> None:
    from mlx_lm.server import process_message_content
    from mlx_lm.tokenizer_utils import load

    tokenizer = cast(
        ChatTokenizer,
        load(
            training_model,
            tokenizer_config_extra={"trust_remote_code": False, "local_files_only": True},
        ),
    )
    conversation = Conversation(
        id=uuid.uuid4(),
        messages=[
            ConversationMessage(
                id=uuid.uuid4(),
                role="user",
                parts=[TextPart(text="Return the sum of two and three. ☃")],
            ),
            ConversationMessage(id=uuid.uuid4(), role="assistant", parts=[TextPart(text="5")]),
        ],
    )
    example = TrainingExample(conversation=conversation, source_row=1)
    result = render_example(example, tokenizer, max_sequence_length=8192, enable_thinking=False)
    messages = [
        message.model_dump(mode="json", exclude_none=True)
        for message in openai_text_messages(conversation)
    ]
    serving = copy.deepcopy(messages)
    cast(Callable[[list[dict[str, Any]]], None], process_message_content)(serving)
    tools = openai_tools(conversation) or None
    completed = tokenizer.apply_chat_template(
        serving, tools=tools, tokenize=True, add_generation_prompt=False, enable_thinking=False
    )
    assert result.tokens == completed
    generation = tokenizer.apply_chat_template(
        copy.deepcopy(serving),
        tools=tools,
        tokenize=True,
        add_generation_prompt=True,
        enable_thinking=False,
    )
    assert generation[: len(completed)] == completed
    assert all(not value for value in result.target_mask[: result.target_start])
    assert any(result.target_mask[result.target_start :])


@pytest.mark.parametrize("enable_thinking", [False, True])
@pytest.mark.parametrize("final_tool", [False, True])
def test_override_tools_multipart_and_metadata_match_actual_serving(
    training_model: Path, enable_thinking: bool, final_tool: bool
) -> None:
    from mlx_lm.server import process_message_content
    from mlx_lm.tokenizer_utils import load

    tokenizer = load(
        training_model,
        tokenizer_config_extra={"trust_remote_code": False, "local_files_only": True},
    )
    # Bare serving binds effective template *content*, not the override file path.
    template = (
        "{{ tools | tojson }}\n{{ 'thinking-on' if enable_thinking else 'thinking-off' }}\n"
        "{% for message in messages %}<|im_start|>{{ message.role }}\n"
        "{{ message.content }}{% if message.tool_calls is defined %}{{ message.tool_calls | tojson }}{% endif %}"
        "<|im_end|>\n{% endfor %}{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}"
    )
    cast(Any, tokenizer).chat_template = template
    conversation = Conversation(
        id=uuid.uuid4(),
        metadata={"note": "excluded-provenance"},
        tools=[
            ConversationTool(
                name="add", parameters={"type": "object", "properties": {"a": {"type": "integer"}}}
            )
        ],
        messages=[
            ConversationMessage(
                id=uuid.uuid4(),
                role="user",
                parts=[TextPart(text="Add "), TextPart(text="two and three ☃")],
            ),
            ConversationMessage(
                id=uuid.uuid4(),
                role="assistant",
                parts=[],
                tool_calls=[ToolCall(id="call-1", name="add", arguments={"a": 2, "b": 3})],
            ),
            ConversationMessage(
                id=uuid.uuid4(), role="tool", parts=[TextPart(text="5")], tool_call_id="call-1"
            ),
            ConversationMessage(
                id=uuid.uuid4(),
                role="assistant",
                parts=[] if final_tool else [TextPart(text="5")],
                tool_calls=[ToolCall(id="call-2", name="add", arguments={"a": 5})]
                if final_tool
                else [],
            ),
        ],
    )
    result = render_example(
        TrainingExample(conversation=conversation, source_row=1),
        cast(ChatTokenizer, tokenizer),
        max_sequence_length=8192,
        enable_thinking=enable_thinking,
    )
    messages = [
        message.model_dump(mode="json", exclude_none=True)
        for message in openai_text_messages(conversation)
    ]
    cast(Callable[[list[dict[str, Any]]], None], process_message_content)(messages)
    assert result.tokens == cast(ChatTokenizer, tokenizer).apply_chat_template(
        messages,
        tools=openai_tools(conversation),
        tokenize=True,
        add_generation_prompt=False,
        enable_thinking=enable_thinking,
    )
    decoded = tokenizer.decode(result.tokens)
    assert "excluded-provenance" not in decoded
    assert "Add two and three" in decoded
    assert result.target_start > 0 and any(result.target_mask)
