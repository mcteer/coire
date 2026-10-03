from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coire_core.models.acquisition import Precision, QuantizationMode, VariantRecipe
from coire_node.conversion import (
    build_convert_argv,
    convert_atomic,
    dequantize_then_convert_atomic,
)


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


def test_fake_conversion_uses_the_same_atomic_publication_path(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "config.json").write_text("{}")
    monkeypatch.setenv("COIRE_TEST_FAKE_CONVERSION", "1")

    convert_atomic(
        source=source,
        destination=target,
        recipe=VariantRecipe(name="bf16", precision=Precision.BF16),
        job_suffix="job",
    )

    assert (target / "config.json").is_file()
    assert not (tmp_path / "target.partial-job").exists()


@pytest.mark.parametrize("dequantize", [False, True])
def test_mlx_convert_receives_absent_output_directory(tmp_path, monkeypatch, dequantize) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    calls = []

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]
        output = Path(argv[argv.index("--mlx-path") + 1])
        assert not output.exists()
        output.mkdir()
        (output / "config.json").write_text("{}")
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"")

    monkeypatch.setattr(subprocess, "run", run)
    operation = dequantize_then_convert_atomic if dequantize else convert_atomic
    operation(
        source=source,
        destination=target,
        recipe=VariantRecipe(name="bf16", precision=Precision.BF16),
        job_suffix="job",
    )

    assert len(calls) == (2 if dequantize else 1)
    assert (target / "config.json").is_file()
    assert not (tmp_path / "target.dequantized-job").exists()


def test_matching_quantization_copies_verified_source_without_loss(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "config.json").write_text(json.dumps({"quantization": {"bits": 4, "group_size": 64}}))
    (source / "tokenizer_config.json").write_text('{"chat_template":"{{ messages }}"}')
    (source / "model.safetensors").write_bytes(b"unchanged weights")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("matching weights must not be requantized")
    )

    dequantize_then_convert_atomic(
        source=source,
        destination=target,
        recipe=VariantRecipe(name="new-name", precision=Precision.BIT4, bits=4, group_size=64),
        job_suffix="job",
    )

    assert (target / "model.safetensors").read_bytes() == b"unchanged weights"
    assert (target / "tokenizer_config.json").read_text() == '{"chat_template":"{{ messages }}"}'
