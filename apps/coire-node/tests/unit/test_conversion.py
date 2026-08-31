from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coire_core.models.acquisition import Precision, QuantizationMode, VariantRecipe
from coire_node.conversion import build_convert_argv, convert_atomic


def test_convert_argv_is_explicit_and_has_no_upload_or_remote_code() -> None:
    recipe = VariantRecipe(
        name="4bit", precision=Precision.BIT4, bits=4, group_size=64, mode=QuantizationMode.AFFINE
    )
    argv = build_convert_argv(Path("/raw"), Path("/partial"), recipe, python="python")
    assert argv[:4] == ["python", "-m", "mlx_lm", "convert"]
    assert "--quantize" in argv
    assert "--q-bits" in argv
    assert not any("upload" in value or "trust" in value for value in argv)


def test_convert_failure_removes_partial_and_never_publishes(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout=b"disk full"),
    )
    with pytest.raises(RuntimeError, match="disk full"):
        convert_atomic(
            source=source,
            destination=target,
            recipe=VariantRecipe(name="bf16", precision=Precision.BF16),
            job_suffix="job",
        )
    assert not target.exists()
    assert not (tmp_path / "target.partial-job").exists()
