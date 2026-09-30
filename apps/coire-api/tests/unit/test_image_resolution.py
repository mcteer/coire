"""A basic image request resolves measured defaults before durable admission."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from coire_api.images.resolution import resolve_basic_image_spec
from coire_core.errors import ImageValidationError
from coire_core.models.images import (
    ImageCapabilityProfile,
    ImageMode,
    ImageSubmitRequest,
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
        "default_guidance": "1.5",
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
        Decimal("1.5"),
    )
    assert result.seed == 123 and canonical_spec_hash(result) == canonical_spec_hash(result)


def test_basic_resolution_preserves_explicit_overrides() -> None:
    request = ImageSubmitRequest(
        model_id=MODEL, prompt="portrait", width=768, steps=12, seed=0, n=2
    )
    result = resolve_basic_image_spec(request, _profile(), random_seed=lambda: 123)
    assert (result.width, result.steps, result.seed, result.n) == (768, 12, 0, 2)


@pytest.mark.parametrize(
    "submission",
    [
        ImageSubmitRequest(model_id=MODEL, prompt="portrait", width=2048),
        ImageSubmitRequest(model_id=MODEL, prompt="portrait", mode=ImageMode.IMG2IMG),
        ImageSubmitRequest(model_id=MODEL, prompt="portrait", variant_id=uuid.uuid4()),
        ImageSubmitRequest(model_id=MODEL, prompt="portrait", negative_prompt="blur"),
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
