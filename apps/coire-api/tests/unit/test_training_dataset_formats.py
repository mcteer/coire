"""Immutable normalized inputs and seed-zero duplicate-group split guarantees."""

import uuid

import pytest
from pydantic import ValidationError

from coire_api.training.dataset_formats import normalize_row, split_rows
from coire_core.models.datasets import DatasetFormat


def test_text_and_prompt_rows_normalize_deterministically_and_keep_provenance_out_of_content() -> (
    None
):
    dataset = uuid.uuid4()
    a = normalize_row(
        {"text": "sample", "metadata": {"note": "provenance"}},
        format=DatasetFormat.TEXT,
        dataset_id=dataset,
        source_row=1,
    )
    b = normalize_row(
        {"text": "sample"}, format=DatasetFormat.TEXT, dataset_id=dataset, source_row=2
    )
    assert a.content_sha256() == b.content_sha256()
    assert a.content_mode == "text" and a.loss_policy == "all_tokens"
    pair = normalize_row(
        {"prompt": "sum two and three", "completion": "5"},
        format=DatasetFormat.PROMPT_COMPLETION,
        dataset_id=dataset,
        source_row=1,
    )
    assert [message.role for message in pair.conversation.messages] == ["user", "assistant"]


def test_images_unknown_fields_and_orphaned_tool_results_are_refused() -> None:
    for row in (
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "image_url", "image_url": {"url": "https://untrusted"}}],
                }
            ]
        },
        {
            "messages": [
                {"role": "tool", "tool_call_id": "unknown", "content": "result"},
                {"role": "assistant", "content": "answer"},
            ]
        },
        {"messages": [{"role": "assistant", "content": "answer"}], "loader_code": "anything"},
    ):
        with pytest.raises(ValidationError):
            normalize_row(
                row, format=DatasetFormat.CONVERSATION, dataset_id=uuid.uuid4(), source_row=1
            )


def test_duplicate_groups_never_cross_splits_and_seed_zero_repeats() -> None:
    dataset = uuid.uuid4()
    hashes = ["a" * 64, "b" * 64, "a" * 64, "c" * 64]
    a = split_rows(dataset, "d" * 64, hashes, seed=0, validation_fraction=0.5)
    b = split_rows(dataset, "d" * 64, hashes, seed=0, validation_fraction=0.5)
    assert a == b
    assert (1 in a.train_rows) == (3 in a.train_rows)
    assert a.train_rows and a.validation_rows
