"""Pinned local file layouts recognized by the native image runtime."""

from __future__ import annotations

from collections.abc import Container

from coire_core.models.registry import ModelKind

SEEDVR2_3B_REPO_ID = "numz/SeedVR2_comfyUI"
SEEDVR2_3B_FILES = frozenset({"seedvr2_ema_3b_fp16.safetensors", "ema_vae_fp16.safetensors"})
CONTROL_UNION_REPO_ID = "alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union-2.1"
CONTROL_UNION_FILE = "Z-Image-Turbo-Fun-Controlnet-Union-2.1.safetensors"
QWEN_EDIT_REPO_IDS = frozenset(
    {
        "Qwen/Qwen-Image-Edit-2509",
        "mflux-community/qwen-image-edit-2509-mflux-q8",
    }
)

FLUX_FILL_REPO_ID = "black-forest-labs/FLUX.1-Fill-dev"


def has_seedvr2_3b_layout(repo_id: str, files: Container[str]) -> bool:
    """The mflux 0.20.0 3B loader consumes these root files without config.json."""
    return repo_id == SEEDVR2_3B_REPO_ID and all(name in files for name in SEEDVR2_3B_FILES)


def has_control_union_layout(repo_id: str, files: Container[str]) -> bool:
    """mflux has a local Union 2.1 fallback config for this exact checkpoint."""
    return repo_id == CONTROL_UNION_REPO_ID and CONTROL_UNION_FILE in files


def include_image_asset_path(repo_id: str, kind: ModelKind, path: str) -> bool:
    """Do not transfer the upstream 7B weights for a pinned 3B upscaler."""
    if kind is ModelKind.CONTROL_MODEL and repo_id == CONTROL_UNION_REPO_ID:
        return path == CONTROL_UNION_FILE or not path.endswith(".safetensors")
    return not (
        kind is ModelKind.UPSCALE_MODEL
        and repo_id == SEEDVR2_3B_REPO_ID
        and path.endswith(".safetensors")
        and path not in SEEDVR2_3B_FILES
    )


def has_qwen_edit_layout(repo_id: str, files: Container[str]) -> bool:
    """Only reviewed native Qwen edit artifacts may use component-index configuration."""
    return repo_id in QWEN_EDIT_REPO_IDS and all(
        name in files
        for name in (
            "transformer/model.safetensors.index.json",
            "text_encoder/model.safetensors.index.json",
            "vae/model.safetensors.index.json",
            "tokenizer/tokenizer.json",
            "tokenizer/tokenizer_config.json",
        )
    )
