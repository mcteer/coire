"""Pinned local file layouts recognized by the native image runtime."""

from __future__ import annotations

from collections.abc import Container

from coire_core.models.registry import ModelKind

SEEDVR2_3B_REPO_ID = "numz/SeedVR2_comfyUI"
SEEDVR2_3B_FILES = frozenset({"seedvr2_ema_3b_fp16.safetensors", "ema_vae_fp16.safetensors"})
CONTROL_UNION_REPO_ID = "alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union-2.1"
CONTROL_UNION_FILE = "Z-Image-Turbo-Fun-Controlnet-Union-2.1.safetensors"


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
