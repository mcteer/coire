"""Fresh-process preference resume matches the uninterrupted full trajectory."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest

pytestmark = pytest.mark.engine


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
@pytest.mark.parametrize("parent", [False, True], ids=["bare", "parent"])
def test_real_process_resume_preserves_policy_optimizer_sampler_rng_and_initial_reference(
    training_model: Path,
    training_kind: Literal["lora", "qlora", "dora"],
    tmp_path: Path,
    objective: str,
    parent: bool,
) -> None:
    import numpy as np

    script = Path(__file__).resolve().parents[4] / "tests/preference_resume_worker.py"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "MLX_TRUST_REMOTE_CODE", "COIRE_API_TOKEN"}
    }
    environment.update(
        COIRE_TEST_MODEL=str(training_model),
        COIRE_TRAINING_PARAMETERIZATION=training_kind,
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
    )
    for mode in ("prepare", "uninterrupted", "pause", "resume"):
        command = [
            sys.executable,
            str(script),
            "--directory",
            str(tmp_path),
            "--objective",
            objective,
            "--mode",
            mode,
        ]
        if parent:
            command.append("--parent")
        result = subprocess.run(
            command, env=environment, capture_output=True, text=True, timeout=180
        )
        assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-6000:]
    baseline = json.loads((tmp_path / "uninterrupted.json").read_text())
    resumed = json.loads((tmp_path / "resume.json").read_text())
    assert baseline["updates"] == resumed["updates"] == list(range(9, 17))
    assert resumed["rng"] == baseline["rng"]
    assert resumed["sampler"] == baseline["sampler"]
    assert resumed["reference_sha256"] == baseline["reference_sha256"]
    assert (
        resumed["reference_sha256"] is not None
        if objective == "dpo"
        else resumed["reference_sha256"] is None
    )
    assert resumed["losses"] == pytest.approx(baseline["losses"], rel=1e-4, abs=1e-5)
    with (
        np.load(tmp_path / "uninterrupted.npz") as expected,
        np.load(tmp_path / "resume.npz") as actual,
    ):
        assert set(actual.files) == set(expected.files)
        assert any(key.startswith("optimizer:") for key in expected.files)
        assert any(key.startswith("policy:") for key in expected.files)
        for key in expected.files:
            np.testing.assert_allclose(actual[key], expected[key], rtol=1e-5, atol=1e-6)
