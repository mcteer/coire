"""Pinned serving primitives plus explicit final-assistant or raw-text supervision."""

from __future__ import annotations

import copy
import platform
from collections.abc import Callable
from typing import Any, Protocol, cast

from opentelemetry import trace

from coire_core.conversation_rendering import openai_text_messages, openai_tools
from coire_core.errors import TrainingValidationError
from coire_core.models.conversation import TextPart
from coire_core.models.datasets import TokenizedTrainingExample, TrainingExample
from coire_core.models.preference import PreferenceRow, TokenizedPreferenceExample

tracer = trace.get_tracer("coire.node.training")


class ChatTokenizer(Protocol):
    @property
    def has_chat_template(self) -> bool: ...

    def encode(self, text: str) -> list[int]: ...

    def apply_chat_template(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        tokenize: bool,
        add_generation_prompt: bool,
        enable_thinking: bool,
    ) -> list[int]: ...


def _prepare_messages(messages: list[dict[str, Any]]) -> None:
    if platform.node().lower().split(".", 1)[0] == "coire-core":
        raise TrainingValidationError("Model/tokenizer rendering is forbidden on core")
    from mlx_lm.server import process_message_content

    # This is the exact in-place primitive used by pinned bare serving, not a second renderer.
    cast(Callable[[list[dict[str, Any]]], None], process_message_content)(messages)


def _render(
    tokenizer: ChatTokenizer,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    generation: bool,
    enable_thinking: bool,
) -> list[int]:
    prepared = copy.deepcopy(messages)
    _prepare_messages(prepared)
    try:
        tokens = tokenizer.apply_chat_template(
            prepared,
            tools=tools,
            tokenize=True,
            add_generation_prompt=generation,
            enable_thinking=enable_thinking,
        )
    except Exception:
        raise TrainingValidationError(
            "SFT conversation is incompatible with the effective template"
        ) from None
    if not isinstance(tokens, list) or any(type(token) is not int for token in tokens):
        raise TrainingValidationError("SFT template did not produce a flat token sequence")
    return tokens


def supervision_tokens(
    example: TrainingExample,
    tokenizer: ChatTokenizer,
    *,
    max_sequence_length: int,
    enable_thinking: bool,
) -> tuple[list[int], int]:
    if not 2 <= max_sequence_length <= 8192:
        raise TrainingValidationError("Invalid training sequence bound")
    with tracer.start_as_current_span("coire.node.training.render"):
        if example.content_mode == "text":
            text = "".join(
                part.text
                for part in example.conversation.messages[0].parts
                if isinstance(part, TextPart)
            )
            tokens = tokenizer.encode(text)
            start = 1
        else:
            if not tokenizer.has_chat_template:
                raise TrainingValidationError(
                    "SFT conversations require a verified shared chat template"
                )
            messages = [
                message.model_dump(mode="json", exclude_none=True)
                for message in openai_text_messages(example.conversation)
            ]
            tools = openai_tools(example.conversation) or None
            tokens = _render(
                tokenizer, messages, tools, generation=False, enable_thinking=enable_thinking
            )
            prefix = _render(
                tokenizer, messages[:-1], tools, generation=True, enable_thinking=enable_thinking
            )
            if tokens[: len(prefix)] != prefix:
                raise TrainingValidationError("SFT assistant token-prefix alignment is unsupported")
            start = len(prefix)
        if len(tokens) < 2 or start < 1 or start >= len(tokens):
            raise TrainingValidationError("Training sample has no supervised target tokens")
        return tokens, start


def render_example(
    example: TrainingExample,
    tokenizer: ChatTokenizer,
    *,
    max_sequence_length: int,
    enable_thinking: bool,
) -> TokenizedTrainingExample:
    tokens, start = supervision_tokens(
        example, tokenizer, max_sequence_length=max_sequence_length, enable_thinking=enable_thinking
    )
    if len(tokens) > max_sequence_length:
        raise TrainingValidationError("Training sample exceeds the declared sequence length")
    return TokenizedTrainingExample(
        source_row=example.source_row,
        content_sha256=example.content_sha256(),
        tokens=tokens,
        target_mask=[index >= start for index in range(len(tokens))],
        target_start=start,
    )


def render_preference_example(
    example: PreferenceRow,
    tokenizer: ChatTokenizer,
    *,
    source_row: int,
    max_sequence_length: int,
    enable_thinking: bool,
) -> TokenizedPreferenceExample:
    if not 2 <= max_sequence_length <= 8192 or not tokenizer.has_chat_template:
        raise TrainingValidationError(
            "Preference rendering requires a bounded shared chat template"
        )
    messages = [message.model_dump(mode="json") for message in example.prompt]
    with tracer.start_as_current_span("coire.node.training.preference.render"):
        prefix = _render(
            tokenizer, messages, None, generation=True, enable_thinking=enable_thinking
        )
        sequences = []
        for answer in (example.chosen, example.rejected):
            tokens = _render(
                tokenizer,
                [*messages, {"role": "assistant", "content": answer}],
                None,
                generation=False,
                enable_thinking=enable_thinking,
            )
            if tokens[: len(prefix)] != prefix:
                raise TrainingValidationError(
                    "Preference assistant token-prefix alignment is unsupported"
                )
            if not prefix or len(prefix) >= len(tokens):
                raise TrainingValidationError("Preference response has no supervised target tokens")
            if len(tokens) > max_sequence_length:
                raise TrainingValidationError(
                    "Preference response exceeds the declared sequence length"
                )
            sequences.append(tokens)
        if sequences[0] == sequences[1]:
            raise TrainingValidationError("Preference responses are token-identical")
        return TokenizedPreferenceExample(
            source_row=source_row,
            content_sha256=example.content_sha256(),
            prompt_sha256=example.prompt_sha256(),
            chosen_tokens=sequences[0],
            rejected_tokens=sequences[1],
            chosen_mask=[index >= len(prefix) for index in range(len(sequences[0]))],
            rejected_mask=[index >= len(prefix) for index in range(len(sequences[1]))],
            prompt_length=len(prefix),
        )
