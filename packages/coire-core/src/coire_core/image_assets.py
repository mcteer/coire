"""Pinned local file layouts recognized by the native image runtime."""

from __future__ import annotations

from collections.abc import Container

from coire_core.models.registry import ModelKind

SEEDVR2_3B_REPO_ID = "numz/SeedVR2_comfyUI"
SEEDVR2_3B_FILES = frozenset({"seedvr2_ema_3b_fp16.safetensors", "ema_vae_fp16.safetensors"})


def has_seedvr2_3b_layout(repo_id: str, files: Container[str]) -> bool:
    """The mflux 0.20.0 3B loader consumes these root files without config.json."""
    return repo_id == SEEDVR2_3B_REPO_ID and all(name in files for name in SEEDVR2_3B_FILES)


def include_image_asset_path(repo_id: str, kind: ModelKind, path: str) -> bool:
    """Do not transfer the upstream 7B weights for a pinned 3B upscaler."""
    return not (
        kind is ModelKind.UPSCALE_MODEL
        and repo_id == SEEDVR2_3B_REPO_ID
        and path.endswith(".safetensors")
        and path not in SEEDVR2_3B_FILES
    )
