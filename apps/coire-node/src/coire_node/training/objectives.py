"""Allowlisted offline SFT preparation over the unchanged bare mlx-lm APIs."""

from __future__ import annotations

import hashlib
import json
import os
import platform
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from opentelemetry import trace

from coire_core.errors import TrainingValidationError
from coire_core.models.jobs import ChecksumManifest
from coire_core.models.training import TrainingParameterization
from coire_node.store import sha256_file

if TYPE_CHECKING:
    import mlx.core as mx
    from mlx.nn.layers.base import Module
    from mlx_lm.tokenizer_utils import TokenizerWrapper

tracer = trace.get_tracer("coire.node.training")
APPROVED_ARCHITECTURES = frozenset({"llama", "qwen2"})
APPROVED_TARGETS = frozenset(
    {
        "self_attn.q_proj",
        "self_attn.k_proj",
        "self_attn.v_proj",
        "self_attn.o_proj",
        "mlp.gate_proj",
        "mlp.up_proj",
        "mlp.down_proj",
    }
)
APPROVED_TOKENIZERS = frozenset(
    {
        "PreTrainedTokenizerFast",
        "LlamaTokenizer",
        "LlamaTokenizerFast",
        "Qwen2Tokenizer",
        "Qwen2TokenizerFast",
    }
)
APPROVED_TOOL_PARSERS = frozenset({"json_tools", "qwen3_coder", "pythonic"})
MAX_CONFIG_BYTES = 64 * 1024


@dataclass(frozen=True)
class SftInput:
    root: Path
    manifest: ChecksumManifest
    config: dict[str, Any]
    parameterization: TrainingParameterization


@dataclass
class SftRuntime:
    model: Module
    tokenizer: TokenizerWrapper
    config: dict[str, Any]
    trainable_keys: frozenset[str]
    frozen_parameters: dict[str, mx.array]

    def parameters(self) -> dict[str, mx.array]:
        getter = cast(Callable[[], dict[str, Any]], self.model.parameters)
        return _flat_parameters(getter())

    def current_frozen_parameters(self) -> dict[str, mx.array]:
        return {
            key: value for key, value in self.parameters().items() if key not in self.trainable_keys
        }


def _flat_parameters(parameters: dict[str, Any]) -> dict[str, mx.array]:
    from mlx.utils import tree_flatten

    return dict(cast(list[tuple[str, "mx.array"]], tree_flatten(parameters)))


def _read_config(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_CONFIG_BYTES:
        raise TrainingValidationError("Required local SFT configuration is missing or unsafe")
    try:
        value = json.loads(path.read_bytes())
    except (ValueError, UnicodeError, RecursionError):
        raise TrainingValidationError("Local SFT configuration is invalid") from None
    if not isinstance(value, dict):
        raise TrainingValidationError("Local SFT configuration must be an object")
    return value


def validate_sft_input(
    root: Path, parameters: TrainingParameterization, *, expected_manifest_sha256: str | None = None
) -> SftInput:
    """Verify inert local bytes before importing a model or tokenizer runtime.

    The production node command supplies its registry-resolved expected manifest digest.
    Standalone fixtures also verify the complete adjacent acquisition manifest locally.
    """
    with tracer.start_as_current_span("coire.node.training.preflight"):
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise TrainingValidationError("SFT requires a contained local acquired model")
        root = root.resolve()
        manifest_path = root.with_name(root.name + ".manifest.json")
        if (
            not manifest_path.is_file()
            or manifest_path.is_symlink()
            or manifest_path.stat().st_size > 4 * 1024**2
        ):
            raise TrainingValidationError("SFT acquisition manifest is missing or unsafe")
        try:
            manifest = ChecksumManifest.model_validate_json(manifest_path.read_bytes())
        except ValueError:
            raise TrainingValidationError("SFT acquisition manifest is invalid") from None
        if (
            manifest.slug != root.name
            or sum(entry.bytes for entry in manifest.files) != manifest.total_bytes
        ):
            raise TrainingValidationError("SFT registry identity differs from its local copy")

        if (
            expected_manifest_sha256 is not None
            and hashlib.sha256(manifest.canonical_bytes()).hexdigest() != expected_manifest_sha256
        ):
            raise TrainingValidationError("SFT manifest differs from the registered base variant")
        listed = {entry.path for entry in manifest.files}
        if (
            len(listed) != len(manifest.files)
            or not {"config.json", "tokenizer_config.json"} <= listed
        ):
            raise TrainingValidationError("SFT manifest is incomplete")
        if not any(
            name.endswith(".safetensors") and Path(name).name.startswith("model") for name in listed
        ):
            raise TrainingValidationError("SFT requires local safetensors weights")
        if not ({"tokenizer.json", "tokenizer.model"} & listed):
            raise TrainingValidationError("SFT tokenizer assets are missing")
        actual: set[str] = set()
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if relative.parts[0] == ".cache":
                continue
            if path.is_symlink():
                raise TrainingValidationError(
                    "SFT model files and directories must not be symlinks"
                )
            if path.is_file():
                actual.add(relative.as_posix())
        if actual != listed:
            raise TrainingValidationError("SFT model tree differs from its acquired manifest")
        for entry in manifest.files:
            path = root / entry.path
            if path.stat().st_size != entry.bytes or sha256_file(path) != entry.sha256:
                raise TrainingValidationError("SFT model checksum verification failed")
        config = _read_config(root / "config.json")
        if config.get("model_file") is not None or config.get("auto_map"):
            raise TrainingValidationError("Executable SFT model configuration is unsupported")
        if (
            not isinstance(config.get("model_type"), str)
            or config["model_type"] not in APPROVED_ARCHITECTURES
            or (
                config.get("quantization_config") is not None
                and config["quantization_config"] != config.get("quantization")
            )
        ):
            raise TrainingValidationError(
                "This SFT architecture or legacy quantization is unsupported"
            )
        layers = config.get("num_hidden_layers")
        if type(layers) is not int or not 1 <= parameters.num_layers <= layers:
            raise TrainingValidationError("Requested SFT layers exceed the approved base")
        if not set(parameters.target_modules) <= APPROVED_TARGETS:
            raise TrainingValidationError("Requested SFT target modules are unsupported")
        quantization = config.get("quantization")
        if parameters.kind == "qlora":
            if (
                not isinstance(quantization, dict)
                or quantization.get("bits") != 4
                or quantization.get("group_size") != 64
                or quantization.get("mode", "affine") != "affine"
                or not set(quantization) <= {"bits", "group_size", "mode"}
            ):
                raise TrainingValidationError(
                    "QLoRA requires an acquired uniform affine 4-bit/group-64 base"
                )
        elif quantization is not None:
            raise TrainingValidationError(
                "LoRA and DoRA initially require an unquantized dense base"
            )
        tokenizer = _read_config(root / "tokenizer_config.json")
        parser = tokenizer.get("tool_parser_type")
        if (
            tokenizer.get("auto_map")
            or not isinstance(tokenizer.get("tokenizer_class"), str)
            or tokenizer["tokenizer_class"] not in APPROVED_TOKENIZERS
            or tokenizer.get("chat_template_type")
            or (
                parser is not None
                and (not isinstance(parser, str) or parser not in APPROVED_TOOL_PARSERS)
            )
        ):
            raise TrainingValidationError(
                "Executable or unapproved SFT tokenizer configuration is unsupported"
            )
        return SftInput(root, manifest, config, parameters)


def load_sft_runtime(source: SftInput, *, seed: int) -> SftRuntime:
    """Only a node-owned native worker/test process on a non-core Mac loads weights."""
    if platform.node().lower().split(".", 1)[0] == "coire-core":
        raise TrainingValidationError("SFT model work is forbidden on core")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise TrainingValidationError("SFT execution requires Apple Silicon")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise TrainingValidationError("SFT seed is outside its declared bound")
    validate_sft_input(
        source.root,
        source.parameterization,
        expected_manifest_sha256=hashlib.sha256(source.manifest.canonical_bytes()).hexdigest(),
    )
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "MLX_TRUST_REMOTE_CODE"):
        os.environ.pop(name, None)
    import mlx.core as mx
    from mlx.nn.layers.linear import Linear
    from mlx.nn.layers.quantized import QuantizedLinear
    from mlx_lm.tokenizer_utils import load as load_tokenizer
    from mlx_lm.tuner.utils import linear_to_lora_layers
    from mlx_lm.utils import load_model

    with tracer.start_as_current_span("coire.node.training.load"):
        mx.random.seed(seed)
        tokenizer = load_tokenizer(
            source.root,
            tokenizer_config_extra={"trust_remote_code": False, "local_files_only": True},
        )
        model, config = load_model(source.root, strict=True)
        model.freeze()
        parameters = source.parameterization
        for layer in model.layers[-parameters.num_layers :]:
            modules = dict(layer.named_modules())
            for target in parameters.target_modules:
                module = modules.get(target)
                expected = QuantizedLinear if parameters.kind == "qlora" else Linear
                if not isinstance(module, expected):
                    raise TrainingValidationError(
                        "SFT target does not match the approved linear layer kind"
                    )
        linear_to_lora_layers(
            model,
            parameters.num_layers,
            {
                "rank": parameters.rank,
                "scale": parameters.scale,
                "dropout": parameters.dropout,
                "keys": parameters.target_modules,
            },
            use_dora=parameters.kind == "dora",
        )
        trainable = frozenset(_flat_parameters(model.trainable_parameters()))
        allowed = (
            (".lora_a", ".lora_b", ".m") if parameters.kind == "dora" else (".lora_a", ".lora_b")
        )
        if not trainable or not all(key.endswith(allowed) for key in trainable):
            raise TrainingValidationError("SFT attempted to train unapproved base parameters")
        frozen = {
            key: value
            for key, value in _flat_parameters(model.parameters()).items()
            if key not in trainable
        }
        return SftRuntime(model, tokenizer, config, trainable, frozen)


class SftObjective:
    name = "sft"
    validate = staticmethod(validate_sft_input)
    load = staticmethod(load_sft_runtime)


def get_objective(name: str) -> type[SftObjective]:
    if name != "sft":
        raise TrainingValidationError("Training objective is unsupported")
    return SftObjective
