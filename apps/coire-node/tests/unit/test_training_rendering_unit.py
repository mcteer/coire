"""Pure mask/serialization boundaries use fake tokenizer primitives, never MLX on core."""

import uuid
from typing import Any

import pytest

from coire_core.conversation_rendering import openai_text_messages
from coire_core.errors import TrainingValidationError
from coire_core.models.conversation import Conversation, ConversationMessage, TextPart, ToolCall
from coire_core.models.datasets import TrainingExample
from coire_node.training.rendering import render_example


class FakeTokenizer:
    has_chat_template = True

    def encode(self, text: str) -> list[int]:
        return [1, *(ord(char) for char in text)]

    def apply_chat_template(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        tokenize: bool,
        add_generation_prompt: bool,
        enable_thinking: bool,
    ) -> list[int]:
        assert tokenize and enable_thinking is False
        text = "".join(f"<{item['role']}>" + item["content"] + "</turn>" for item in messages)
        if add_generation_prompt:
            text += "<assistant>"
        return self.encode(text)


def example(text: str = "5") -> TrainingExample:
    return TrainingExample(
        conversation=Conversation(
            id=uuid.uuid4(),
            messages=[
                ConversationMessage(
                    id=uuid.uuid4(), role="user", parts=[TextPart(text="Sum two and three.")]
                ),
                ConversationMessage(id=uuid.uuid4(), role="assistant", parts=[TextPart(text=text)]),
            ],
        ),
        source_row=1,
    )


def fake_prepare(messages: list[dict[str, Any]]) -> None:
    for message in messages:
        if isinstance(message.get("content"), list):
            message["content"] = "".join(part["text"] for part in message["content"])
        elif message.get("content") is None:
            message["content"] = ""


def test_supervision_only_covers_the_final_assistant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coire_node.training.rendering._prepare_messages", fake_prepare)
    rendered = render_example(
        example(), FakeTokenizer(), max_sequence_length=8192, enable_thinking=False
    )
    assert sum(rendered.target_mask) > 0
    assert not any(rendered.target_mask[: rendered.target_start])
    assert len(rendered.target_mask) == len(rendered.tokens)


def test_overlength_and_incompatible_prefix_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coire_node.training.rendering._prepare_messages", fake_prepare)
    with pytest.raises(TrainingValidationError, match="sequence"):
        render_example(example(), FakeTokenizer(), max_sequence_length=4, enable_thinking=False)

    class BrokenPrefix(FakeTokenizer):
        def apply_chat_template(self, messages: list[dict[str, Any]], **kwargs: Any) -> list[int]:
            return [999] if kwargs["add_generation_prompt"] else [1, 2, 3]

    with pytest.raises(TrainingValidationError, match="alignment"):
        render_example(example(), BrokenPrefix(), max_sequence_length=8192, enable_thinking=False)


def test_raw_text_has_no_chat_framing() -> None:
    data = TrainingExample(
        conversation=Conversation(
            id=uuid.uuid4(),
            messages=[
                ConversationMessage(
                    id=uuid.uuid4(), role="assistant", parts=[TextPart(text="plain")]
                )
            ],
        ),
        source_row=1,
        content_mode="text",
        loss_policy="all_tokens",
    )
    result = render_example(data, FakeTokenizer(), max_sequence_length=8192, enable_thinking=False)
    assert result.tokens == FakeTokenizer().encode("plain")
    assert result.target_start == 1 and result.target_mask[0] is False


def test_tool_arguments_remain_wire_strings_and_metadata_is_omitted() -> None:
    message = ConversationMessage(
        id=uuid.uuid4(),
        role="assistant",
        parts=[],
        tool_calls=[ToolCall(id="call-1", name="add", arguments={"b": 3, "a": 2})],
        metadata={"note": "not model input"},
    )
    conversation = Conversation(id=uuid.uuid4(), messages=[message])
    encoded = openai_text_messages(conversation)[0].model_dump(mode="json", exclude_none=True)
    assert encoded["tool_calls"][0]["function"]["arguments"] == '{"a":2,"b":3}'
    assert "metadata" not in encoded and "id" not in encoded
