"""Fake-tokenizer tests exercise paired boundaries without native work on core."""

from typing import Any

import pytest

from coire_core.errors import TrainingValidationError
from coire_core.models.preference import PreferenceRow
from coire_node.training import rendering


class Tokenizer:
    has_chat_template = True

    def encode(self, text: str) -> list[int]:
        return [ord(char) for char in text]

    def apply_chat_template(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: object,
        tokenize: bool,
        add_generation_prompt: bool,
        enable_thinking: bool,
    ) -> list[int]:
        text = "".join(message["role"] + ":" + message["content"] + ";" for message in messages)
        return self.encode(text + ("assistant:" if add_generation_prompt else ""))


def test_both_responses_select_termination_and_share_exact_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rendering, "_prepare_messages", lambda messages: None)
    row = PreferenceRow.model_validate(
        {"prompt": [{"role": "user", "content": "hi"}], "chosen": "yes", "rejected": "no"}
    )
    example = rendering.render_preference_example(
        row, Tokenizer(), source_row=1, max_sequence_length=100, enable_thinking=False
    )
    assert (
        example.chosen_tokens[: example.prompt_length]
        == example.rejected_tokens[: example.prompt_length]
    )
    assert example.chosen_mask[-1] and example.chosen_tokens[-1] == ord(";")
    assert sum(example.chosen_mask) == 4 and sum(example.rejected_mask) == 3
    with pytest.raises(TrainingValidationError):
        rendering.render_preference_example(
            row, Tokenizer(), source_row=1, max_sequence_length=21, enable_thinking=False
        )


def test_token_identical_answers_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rendering, "_prepare_messages", lambda messages: None)

    class FoldingTokenizer(Tokenizer):
        def encode(self, text: str) -> list[int]:
            return super().encode(text.lower())

    row = PreferenceRow.model_validate(
        {"prompt": [{"role": "user", "content": "hi"}], "chosen": "YES", "rejected": "yes"}
    )
    with pytest.raises(TrainingValidationError, match="token-identical"):
        rendering.render_preference_example(
            row, FoldingTokenizer(), source_row=1, max_sequence_length=100, enable_thinking=False
        )
