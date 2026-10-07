"""Explicitly offline engine fixtures. Enabled missing prerequisites fail, not skip."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

import pytest

from coire_node.testing.training import offline_training_model


@pytest.fixture
def training_model(monkeypatch: pytest.MonkeyPatch) -> Path:
    if os.environ.get("COIRE_TRAINING_ENGINE") != "1":
        pytest.skip("opt in with COIRE_TRAINING_ENGINE=1 on a non-core Apple Silicon Mac")
    try:
        path = offline_training_model(os.environ.get("COIRE_TEST_MODEL"))
    except (ValueError, OSError) as error:
        pytest.fail(str(error))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "false")
    for variable in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "MLX_TRUST_REMOTE_CODE"):
        monkeypatch.delenv(variable, raising=False)
    return path


@pytest.fixture
def training_kind(training_model: Path) -> Literal["lora", "qlora", "dora"]:
    requested = os.environ.get("COIRE_TRAINING_PARAMETERIZATION")
    if requested is not None:
        if requested not in {"lora", "qlora", "dora"}:
            pytest.fail("unsupported numerical gate parameterization")
        if requested == "lora":
            return "lora"
        if requested == "qlora":
            return "qlora"
        return "dora"
    config = json.loads((training_model / "config.json").read_bytes())
    return "qlora" if config.get("quantization") is not None else "lora"


@pytest.fixture
def training_replica_roots(tmp_path: Path) -> tuple[Path, Path]:
    roots = (tmp_path / "replica-a", tmp_path / "replica-b")
    for root in roots:
        root.mkdir(mode=0o700)
    return roots
