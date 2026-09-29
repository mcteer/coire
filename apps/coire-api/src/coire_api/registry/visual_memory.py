"""Conservative Studio memory admission for measured visual models."""

from __future__ import annotations

from coire_core.models.registry import EngineBackend, VisualCapability

MIN_IMAGE_WORKING_BYTES = 64 * 1024 * 1024
BYTES_PER_INPUT_PIXEL = 16


def reservation_bytes(base_bytes: int, visual: dict[str, object] | None) -> int:
    """Include measured encoder/cache and bounded input working memory in admissions."""

    base = max(1, base_bytes)
    if visual is None:
        return base
    capability = VisualCapability.model_validate(visual)
    if not capability.verified:
        return base
    input_working = max(
        MIN_IMAGE_WORKING_BYTES,
        capability.max_images * capability.max_image_pixels * BYTES_PER_INPUT_PIXEL,
    )
    return base + capability.encoder_memory_bytes + capability.cache_memory_bytes + input_working


def require_supported_placement(backend: str | EngineBackend, policy: str) -> None:
    if EngineBackend(backend) is EngineBackend.MLX_VLM and policy.startswith("sharded:"):
        raise ValueError("visual models require a single Studio")
