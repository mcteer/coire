"""Offline, local-only smoke validation for a preconverted MLX visual variant."""

from __future__ import annotations

import json
import logging
import os
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
    if (
        not isinstance(architectures, list)
        or "Idefics3ForConditionalGeneration" not in architectures
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
        import mlx.core as mx
        from mlx_vlm import generate as mlx_generate  # type: ignore[attr-defined]
        from mlx_vlm import load as mlx_load  # type: ignore[attr-defined]
        from PIL import Image

        phase = "load"
        mx.metal.reset_peak_memory()
        model, processor = mlx_load(str(model_path), trust_remote_code=False, strict=True)
        phase = "generate"
        with tempfile.TemporaryDirectory(prefix="coire-visual-smoke-") as temporary:
            image_path = Path(temporary) / "fixture.png"
            Image.new("RGB", (16, 16), color=(255, 0, 0)).save(image_path)
            encoded_bytes = image_path.stat().st_size
            result = cast(
                Any,
                mlx_generate(
                    model,
                    cast(Any, processor),
                    "<image>\nDescribe the single colored square in this image.",
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
            max_image_pixels=256,
            max_encoded_bytes=encoded_bytes,
            encoder_memory_bytes=mx.metal.get_peak_memory(),
            cache_memory_bytes=mx.metal.get_cache_memory(),
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
