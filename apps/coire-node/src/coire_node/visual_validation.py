"""Offline, local-only smoke validation for a preconverted MLX visual variant."""

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
from pathlib import Path
from typing import Any, cast

from coire_core.models.acquisition import ValidationOutcome
from coire_core.models.registry import VisualCapability
from coire_node.validation import output_is_nondegenerate

REQUIRED_FILES = frozenset(
    {
        "config.json",
        "processor_config.json",
        "preprocessor_config.json",
        "tokenizer_config.json",
        "tokenizer.json",
    }
)
logger = logging.getLogger(__name__)
SUPPORTED_ARCHITECTURES = frozenset(
    {"Idefics3ForConditionalGeneration", "Qwen4ExpForConditionalGeneration"}
)


def inspect_local_variant(model_path: Path) -> str | None:
    """Reject incomplete, unsupported, or linked files before invoking a loader."""
    if not model_path.is_dir() or model_path.is_symlink():
        return "local visual variant is missing or linked"
    entries = list(model_path.rglob("*"))
    if any(entry.is_symlink() for entry in entries):
        return "local visual variant contains a symbolic link"
    if not REQUIRED_FILES.issubset({entry.name for entry in entries if entry.is_file()}):
        return "local visual processor or tokenizer files are incomplete"
    if not any(entry.suffix == ".safetensors" and entry.is_file() for entry in entries):
        return "local visual weights are missing"
    try:
        config = json.loads((model_path / "config.json").read_text())
    except (OSError, ValueError):
        return "local visual configuration is unreadable"
    architectures = config.get("architectures")
    if not isinstance(architectures, list) or not any(
        architecture in SUPPORTED_ARCHITECTURES for architecture in architectures
    ):
        return "local visual architecture is unsupported"
    return None


def run_visual_smoke(
    model_path: Path,
) -> tuple[ValidationOutcome, str | None, VisualCapability | None]:
    """Exercise one generated image; no repository ID or remote code reaches the loader."""
    failure = inspect_local_variant(model_path)
    if failure:
        return ValidationOutcome.FAIL, failure, None
    # This worker is its own process. Make every Hugging Face/Transformers lookup offline and
    # remove credentials before importing the processor, which may read environment at import.
    managed_keys = (
        "HF_TOKEN",
        "HF_API_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
    )
    previous = {key: os.environ.get(key) for key in managed_keys}
    for key in managed_keys[:3]:
        os.environ.pop(key, None)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    phase = "import"
    try:
        import importlib

        import mlx.core as mx
        from PIL import Image

        mlx_module = importlib.import_module("mlx_vlm")
        mlx_generate = mlx_module.generate
        mlx_load = mlx_module.load
        mlx_apply_chat_template = mlx_module.apply_chat_template

        phase = "load"
        mx.metal.reset_peak_memory()
        model, processor = mlx_load(str(model_path), trust_remote_code=False, strict=True)
        mx.metal.reset_peak_memory()
        baseline_peak = mx.metal.get_peak_memory()
        baseline_cache = mx.metal.get_cache_memory()
        # The variant reservation already includes its serialized weights and load
        # overhead. Charge only the extra visual working set, including lazy loads.
        serialized_weights = sum(path.stat().st_size for path in model_path.rglob("*.safetensors"))
        phase = "generate"
        prompt = mlx_apply_chat_template(
            processor,
            model.config,
            "Describe this image.",
            num_images=1,
        )
        with tempfile.TemporaryDirectory(prefix="coire-visual-smoke-") as temporary:
            image_path = Path(temporary) / "fixture.png"
            pixels = random.Random(0).randbytes(512 * 512 * 3)
            Image.frombytes("RGB", (512, 512), pixels).save(image_path)
            encoded_bytes = image_path.stat().st_size
            result = cast(
                Any,
                mlx_generate(
                    model,
                    cast(Any, processor),
                    prompt,
                    image=str(image_path),
                    max_tokens=32,
                    verbose=False,
                ),
            )
        if not output_is_nondegenerate(str(result.text)):
            return ValidationOutcome.FAIL, "visual generation produced degenerate output", None
        phase = "measure"
        capability = VisualCapability(
            verified=True,
            max_images=1,
            max_image_pixels=512 * 512,
            max_encoded_bytes=encoded_bytes,
            encoder_memory_bytes=max(
                0, mx.metal.get_peak_memory() - max(baseline_peak, serialized_weights)
            ),
            cache_memory_bytes=max(0, mx.metal.get_cache_memory() - baseline_cache),
        )
        return ValidationOutcome.PASS, None, capability
    except Exception as exc:
        logger.exception("visual smoke failed during %s", phase)
        reason = f"visual {phase} failed: {type(exc).__name__}"
        if isinstance(exc, ValueError):
            reason += f": {str(exc)[:200]}"
        return ValidationOutcome.FAIL, reason, None
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
