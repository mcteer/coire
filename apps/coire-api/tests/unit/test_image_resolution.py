"""A basic image request resolves measured defaults before durable admission."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from coire_api.images.resolution import resolve_basic_image_spec
from coire_core.errors import ImageValidationError
from coire_core.models.images import (
    ImageCapabilityProfile,
    ImageControl,
    ImageLora,
    ImageMode,
    ImageSubmitRequest,
    ImageUpscale,
    canonical_spec_hash,
)

MODEL = uuid.uuid4()


def _profile(**updates: object) -> ImageCapabilityProfile:
    values: dict[str, object] = {
        "modes": ["txt2img"],
        "min_width": 256,
        "max_width": 1024,
        "min_height": 256,
        "max_height": 1024,
        "max_pixels": 1024 * 1024,
        "min_steps": 2,
        "max_steps": 30,
        "min_guidance": "0",
        "max_guidance": "4",
        "max_outputs": 2,
        "default_width": 512,
        "default_height": 512,
        "default_steps": 9,
        "default_guidance": "0",
    }
    values.update(updates)
    return ImageCapabilityProfile.model_validate(values)


def test_basic_resolution_uses_measured_defaults_and_one_random_seed() -> None:
    request = ImageSubmitRequest(model_id=MODEL, prompt="portrait")
    result = resolve_basic_image_spec(request, _profile(), random_seed=lambda: 123)
    assert result.model_id == MODEL and result.mode is ImageMode.TXT2IMG
    assert (result.width, result.height, result.steps, result.guidance) == (
        512,
        512,
        9,
        Decimal(0),
    )
    assert result.seed == 123 and canonical_spec_hash(result) == canonical_spec_hash(result)


def test_basic_resolution_preserves_explicit_overrides() -> None:
    request = ImageSubmitRequest(
        model_id=MODEL, prompt="portrait", width=768, steps=12, seed=0, n=2
    )
    result = resolve_basic_image_spec(request, _profile(), random_seed=lambda: 123)
    assert (result.width, result.steps, result.seed, result.n) == (768, 12, 0, 2)


def test_img2img_resolution_preserves_full_precision_strength_and_bound_input() -> None:
    input_id = uuid.uuid4()
    request = ImageSubmitRequest(
        model_id=MODEL,
        prompt="portrait",
        mode=ImageMode.IMG2IMG,
        init_image_id=input_id,
        strength=Decimal("0.375125"),
        seed=0,
    )
    result = resolve_basic_image_spec(
        request, _profile(modes=["txt2img", "img2img"]), random_seed=lambda: 123
    )
    assert result.mode is ImageMode.IMG2IMG
    assert result.init_image_id == input_id and result.strength == Decimal("0.375125")
    assert result.seed == 0
    with pytest.raises(ImageValidationError, match="mode is unsupported"):
        resolve_basic_image_spec(request, _profile(), random_seed=lambda: 123)


def test_resolution_preserves_ordered_lora_scales_and_capability_limit() -> None:
    adapters = [
        ImageLora(model_id=uuid.uuid4(), scale=Decimal("0.375125")),
        ImageLora(model_id=uuid.uuid4(), scale=Decimal("-1.00001")),
    ]
    request = ImageSubmitRequest(model_id=MODEL, prompt="portrait", loras=adapters)
    result = resolve_basic_image_spec(request, _profile(max_loras=2), random_seed=lambda: 123)
    assert result.loras == tuple(adapters)
    with pytest.raises(ImageValidationError, match="loras exceed model bound"):
        resolve_basic_image_spec(request, _profile(max_loras=1), random_seed=lambda: 123)


def test_resolution_keeps_exact_upscale_asset_and_factor() -> None:
    upscale = ImageUpscale(model_id=uuid.uuid4(), factor=2)
    request = ImageSubmitRequest(model_id=MODEL, prompt="portrait", upscale=upscale)
    resolved = resolve_basic_image_spec(request, _profile(), random_seed=lambda: 123)
    assert resolved.upscale == upscale


def test_resolution_preserves_control_thresholds_and_bound_source() -> None:
    control = ImageControl(
        image_id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        strength=Decimal("0.375125"),
        low_threshold=23,
        high_threshold=89,
    )
    request = ImageSubmitRequest(
        model_id=MODEL, prompt="portrait", mode=ImageMode.CONTROL, control=control
    )
    profile = _profile(modes=["txt2img", "control"])
    resolved = resolve_basic_image_spec(request, profile, random_seed=lambda: 123)
    assert resolved.control == control
    assert resolved.mode is ImageMode.CONTROL
    with pytest.raises(ImageValidationError, match="control mode does not support a LoRA stack"):
        resolve_basic_image_spec(
            request.model_copy(
                update={"loras": [ImageLora(model_id=uuid.uuid4(), scale=Decimal("0.5"))]}
            ),
            _profile(modes=["txt2img", "control"], max_loras=1),
            random_seed=lambda: 123,
        )


@pytest.mark.parametrize(
    "submission",
    [
        ImageSubmitRequest(model_id=MODEL, prompt="portrait", width=2048),
        ImageSubmitRequest(model_id=MODEL, prompt="portrait", mode=ImageMode.IMG2IMG),
        ImageSubmitRequest(model_id=MODEL, prompt="portrait", variant_id=uuid.uuid4()),
        ImageSubmitRequest(model_id=MODEL, prompt="portrait", negative_prompt="blur"),
        ImageSubmitRequest(model_id=MODEL, prompt="portrait", guidance=Decimal("1.5")),
    ],
)
def test_basic_resolution_refuses_unavailable_fields(submission: ImageSubmitRequest) -> None:
    with pytest.raises(ImageValidationError):
        resolve_basic_image_spec(submission, _profile(), random_seed=lambda: 123)


def test_basic_resolution_refuses_old_profile_without_defaults() -> None:
    profile = _profile()
    old = ImageCapabilityProfile.model_validate(
        profile.model_dump(
            exclude={"default_width", "default_height", "default_steps", "default_guidance"}
        )
    )
    with pytest.raises(ImageValidationError):
        resolve_basic_image_spec(
            ImageSubmitRequest(model_id=MODEL, prompt="portrait"), old, random_seed=lambda: 123
        )


def test_basic_resolution_refuses_unsupported_registry_default() -> None:
    with pytest.raises(ImageValidationError):
        resolve_basic_image_spec(
            ImageSubmitRequest(model_id=MODEL, prompt="portrait"),
            _profile(default_guidance="1.5"),
            random_seed=lambda: 123,
        )


def test_basic_resolution_refuses_negative_prompt_even_if_profile_advertises_it() -> None:
    with pytest.raises(ImageValidationError):
        resolve_basic_image_spec(
            ImageSubmitRequest(model_id=MODEL, prompt="portrait", negative_prompt="blur"),
            _profile(supports_negative_prompt=True),
            random_seed=lambda: 123,
        )
