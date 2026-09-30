"""Measured visual capacity is reserved before Studio engine admission."""

import pytest

from coire_api.registry.visual_memory import require_supported_placement, reservation_bytes
from coire_core.models.registry import EngineBackend, VisualCapability


def test_visual_reservation_includes_encoder_cache_and_input_working_memory() -> None:
    base = 300 * 1024 * 1024
    measured = VisualCapability(
        verified=True,
        max_images=1,
        max_image_pixels=256,
        max_encoded_bytes=90,
        encoder_memory_bytes=936_487_174,
        cache_memory_bytes=123_984_430,
    )
    required = reservation_bytes(base, measured.model_dump())
    assert required == base + 936_487_174 + 123_984_430 + 64 * 1024 * 1024
    assert reservation_bytes(base, None) == base
    assert (
        reservation_bytes(base, measured.model_copy(update={"verified": False}).model_dump())
        == base
    )


def test_visual_input_working_memory_scales_with_measured_image_limit() -> None:
    visual = VisualCapability(
        verified=True,
        max_images=10,
        max_image_pixels=4_000_000,
        max_encoded_bytes=1024,
    )
    assert reservation_bytes(1, visual.model_dump()) == 1 + 10 * 4_000_000 * 16


def test_visual_backend_refuses_sharding_before_placement() -> None:
    with pytest.raises(ValueError, match="single Studio"):
        require_supported_placement(EngineBackend.MLX_VLM, "sharded:tensor_parallel")
    require_supported_placement(EngineBackend.MLX_VLM, "single")
    require_supported_placement(EngineBackend.MLX_LM, "sharded:tensor_parallel")
