"""SFT metadata safety runs without importing MLX or allocating model weights."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coire_core.errors import TrainingValidationError
from coire_core.models.jobs import ChecksumManifest, ManifestFile
from coire_core.models.training import TrainingParameterization
from coire_node.training.objectives import get_objective, load_sft_runtime, validate_sft_input


def make_source(
    root: Path, config: dict[str, object], tokenizer: dict[str, object] | None = None
) -> Path:
    model = root / "synthetic--sft"
    model.mkdir(exist_ok=True)
    payloads = {
        "config.json": json.dumps(config).encode(),
        "tokenizer_config.json": json.dumps(
            tokenizer or {"tokenizer_class": "PreTrainedTokenizerFast"}
        ).encode(),
        "tokenizer.json": b"{}",
        "model.safetensors": b"metadata-only-test-weights",
    }
    files = []
    for name, data in payloads.items():
        (model / name).write_bytes(data)
        files.append(
            ManifestFile(path=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        )
    manifest = ChecksumManifest(
        slug=model.name,
        repo_id="synthetic/sft",
        revision="local-fixture",
        files=files,
        total_bytes=sum(len(value) for value in payloads.values()),
        created_at=datetime.now(UTC),
    )
    model.with_name(model.name + ".manifest.json").write_text(manifest.model_dump_json())
    return model


def parameters(kind: str = "lora") -> TrainingParameterization:
    return TrainingParameterization.model_validate(
        {"kind": kind, "target_modules": ["self_attn.q_proj"]}
    )


@pytest.mark.parametrize(
    "override",
    [
        {"model_file": "model.py"},
        {"auto_map": {"AutoModel": "model.Custom"}},
        {"model_type": "unapproved"},
        {"model_type": "qwen2_moe"},
    ],
)
def test_executable_or_unapproved_model_configuration_is_refused(
    tmp_path: Path, override: dict[str, object]
) -> None:
    root = make_source(tmp_path, {"model_type": "llama", "num_hidden_layers": 2, **override})
    with pytest.raises(TrainingValidationError):
        validate_sft_input(root, parameters())


@pytest.mark.parametrize(
    "config",
    [
        {"auto_map": {"AutoTokenizer": "local.Custom"}},
        {"tokenizer_class": "CustomTokenizer"},
        {"tool_parser_type": "arbitrary.import"},
        {"chat_template_type": "custom"},
    ],
)
def test_tokenizer_code_and_dynamic_import_selectors_are_refused(
    tmp_path: Path, config: dict[str, object]
) -> None:
    root = make_source(tmp_path, {"model_type": "llama", "num_hidden_layers": 2}, config)
    with pytest.raises(TrainingValidationError):
        validate_sft_input(root, parameters())


def test_quantized_base_only_uses_approved_qlora_configuration(tmp_path: Path) -> None:
    root = make_source(
        tmp_path,
        {
            "model_type": "llama",
            "num_hidden_layers": 2,
            "quantization": {"bits": 4, "group_size": 64},
        },
    )
    assert validate_sft_input(root, parameters("qlora"))
    for kind in ("lora", "dora"):
        with pytest.raises(TrainingValidationError):
            validate_sft_input(root, parameters(kind))


@pytest.mark.parametrize(
    "legacy",
    [{"bits": 4, "group_size": 64}, {"bits": 8, "group_size": 64}, {"quant_method": "gptq"}],
)
def test_quantization_alias_must_exactly_match_acquired_mlx_config(
    tmp_path: Path, legacy: dict[str, object]
) -> None:
    quantization = {"bits": 4, "group_size": 64}
    root = make_source(
        tmp_path,
        {
            "model_type": "qwen2",
            "num_hidden_layers": 2,
            "quantization": quantization,
            "quantization_config": legacy,
        },
    )
    if legacy == quantization:
        assert validate_sft_input(root, parameters("qlora"))
    else:
        with pytest.raises(TrainingValidationError):
            validate_sft_input(root, parameters("qlora"))


def test_linked_or_unmanifested_files_and_changed_bytes_are_refused(tmp_path: Path) -> None:
    root = make_source(tmp_path, {"model_type": "llama", "num_hidden_layers": 2})
    (root / "extra.py").write_text("raise RuntimeError('never execute')")
    with pytest.raises(TrainingValidationError):
        validate_sft_input(root, parameters())
    (root / "extra.py").unlink()
    (root / "model.safetensors").write_bytes(b"changed")
    with pytest.raises(TrainingValidationError):
        validate_sft_input(root, parameters())


def test_dense_lora_dora_and_quantized_matrix_are_explicit(tmp_path: Path) -> None:
    root = make_source(tmp_path, {"model_type": "llama", "num_hidden_layers": 2})
    for kind in ("lora", "dora"):
        assert validate_sft_input(root, parameters(kind)).parameterization.kind == kind
    with pytest.raises(TrainingValidationError, match="QLoRA"):
        validate_sft_input(root, parameters("qlora"))


def test_unsupported_targets_objectives_and_core_execution_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_source(tmp_path, {"model_type": "llama", "num_hidden_layers": 2})
    with pytest.raises(TrainingValidationError):
        validate_sft_input(root, TrainingParameterization(target_modules=["embedding"]))
    with pytest.raises(TrainingValidationError):
        get_objective("dpo")
    source = validate_sft_input(root, parameters())
    monkeypatch.setattr("coire_node.training.objectives.platform.node", lambda: "coire-core.lab")
    with pytest.raises(TrainingValidationError, match="core"):
        load_sft_runtime(source, seed=0)
