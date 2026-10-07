"""Opt-in CPU tokenizer acceptance; never execute this model work on core."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from coire_core.models.datasets import DatasetAnalysisBinding, DatasetFormat
from coire_core.models.jobs import ChecksumManifest
from coire_core.models.training_node import DatasetAnalysisWorkerInput
from coire_node.training.datasets import analyze_source, load_analysis_tokenizer

pytestmark = pytest.mark.engine


def test_offline_analysis_loads_tokenizer_without_model_loader(
    training_model: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mlx_lm import utils

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("dataset analysis attempted to load base weights")

    monkeypatch.setattr(utils, "load_model", forbidden)
    data = b'{"text":"one two three"}\n{"text":"one two three"}\n{"text":"four five six"}\n'
    source = tmp_path / "source.jsonl"
    source.write_bytes(data)
    manifest = ChecksumManifest.model_validate_json(
        training_model.with_name(training_model.name + ".manifest.json").read_bytes()
    )
    binding = DatasetAnalysisBinding(
        dataset_id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        base_manifest_sha256=hashlib.sha256(manifest.canonical_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(data).hexdigest(),
        split_sha256="c" * 64,
        format=DatasetFormat.TEXT,
        model_slug=training_model.name,
    )
    tokenizer, tokenizer_sha, template_sha, runtime_sha = load_analysis_tokenizer(
        training_model, binding
    )
    envelope = DatasetAnalysisWorkerInput(
        command_id=uuid.uuid4(),
        analysis_id=uuid.uuid4(),
        binding=binding,
        source_bytes=len(data),
        memory_bytes=1024**3,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    result = analyze_source(
        source,
        envelope,
        tokenizer,
        tokenizer_sha256=tokenizer_sha,
        template_sha256=template_sha,
        runtime_sha256=runtime_sha,
    )
    assert result.state == "succeeded" and result.row_count == 3 and result.duplicate_rows == 1
    assert result.tokens is not None and sum(result.tokens.histogram) == 3
