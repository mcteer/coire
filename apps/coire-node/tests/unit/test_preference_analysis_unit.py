"""Two complete responses drive tokenizer-only readiness and safe diagnostics."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from coire_core.models.training_node import DatasetAnalysisWorkerInput
from coire_node.training import rendering
from coire_node.training.datasets import analyze_source


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
        return self.encode(
            "".join(message["role"] + ":" + message["content"] + ";" for message in messages)
            + ("assistant:" if add_generation_prompt else "")
        )


def command(data: bytes) -> DatasetAnalysisWorkerInput:
    return DatasetAnalysisWorkerInput.model_validate(
        {
            "command_id": str(uuid.uuid4()),
            "analysis_id": str(uuid.uuid4()),
            "source_bytes": len(data),
            "memory_bytes": 1024,
            "deadline": datetime.now(UTC) + timedelta(minutes=1),
            "binding": {
                "dataset_id": str(uuid.uuid4()),
                "model_id": str(uuid.uuid4()),
                "variant_id": str(uuid.uuid4()),
                "base_manifest_sha256": "a" * 64,
                "source_sha256": hashlib.sha256(data).hexdigest(),
                "split_sha256": "b" * 64,
                "format": "preference",
                "model_slug": "fixture",
            },
        }
    )


def test_analysis_includes_both_answers_and_rejects_either_overlength(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rendering, "_prepare_messages", lambda messages: None)
    data = (
        b"\n".join(
            json.dumps(
                {
                    "prompt": [{"role": "user", "content": prompt}],
                    "chosen": "yes",
                    "rejected": "long alternative",
                }
            ).encode()
            for prompt in ("a", "b")
        )
        + b"\n"
    )
    source = tmp_path / "source.jsonl"
    source.write_bytes(data)
    request = command(data)
    result = analyze_source(
        source,
        request,
        Tokenizer(),
        tokenizer_sha256="c" * 64,
        template_sha256="d" * 64,
        runtime_sha256="e" * 64,
    )
    assert result.state == "succeeded" and result.preference is not None
    assert result.preference.prompt_group_count == 2
    assert result.preference.chosen_response_tokens.total == 8
    assert result.preference.rejected_response_tokens.total == 34
    assert result.tokens is not None and result.tokens.maximum > result.tokens.minimum
    failed = analyze_source(
        source,
        request,
        Tokenizer(),
        tokenizer_sha256="c" * 64,
        template_sha256="d" * 64,
        runtime_sha256="e" * 64,
        max_sequence_length=24,
    )
    assert failed.state == "failed" and failed.invalid_count == 2 and failed.preference is None
    assert all(diagnostic.code == "overlength" for diagnostic in failed.diagnostics)
    assert "alternative" not in str(failed.diagnostics)
