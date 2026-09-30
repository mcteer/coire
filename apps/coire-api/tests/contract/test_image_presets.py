"""Preset resolution keeps immutable policy even when clients override defaults."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from decimal import Decimal

import pytest

from coire_api.images.presets import PresetResolution, resolve_image_preset
from coire_core.errors import ImageConflict, ImageValidationError
from coire_core.models.images import (
    ImageContentMode,
    ImageLora,
    ImagePreset,
    ImageSubmitRequest,
)

MODEL = uuid.uuid4()
LORA = uuid.uuid4()
PRESET_ID = uuid.uuid4()


def _preset(*, retired: bool = False) -> ImagePreset:
    return ImagePreset(
        id=PRESET_ID,
        revision=3,
        name="Portrait",
        prompt_prefix="studio portrait",
        defaults=ImageSubmitRequest(
            model_id=MODEL,
            prompt="a person",
            width=512,
            height=512,
            guidance=Decimal("2.25"),
            loras=[ImageLora(model_id=LORA, scale=Decimal("0.75"))],
            content_mode=ImageContentMode.EXPLICIT,
        ),
        retired=retired,
    )


def _resolve(
    request: ImageSubmitRequest,
    *,
    published: bool = True,
    registry_requirements: Mapping[uuid.UUID, frozenset[str]] | None = None,
) -> PresetResolution:
    return resolve_image_preset(
        request,
        _preset(),
        published=published,
        preset_dependency_ids=frozenset({LORA}),
        preset_requirements=frozenset({"explicit"}),
        registry_requirements=registry_requirements
        if registry_requirements is not None
        else {MODEL: frozenset({"base"}), LORA: frozenset({"adapter"})},
    )


def test_overrides_preserve_decimal_prefix_once_and_policy_union() -> None:
    request = ImageSubmitRequest(
        preset_id=PRESET_ID,
        preset_revision=3,
        prompt="new subject",
        guidance=Decimal("3.125"),
        loras=[],
        content_mode=ImageContentMode.STANDARD,
    )
    result = _resolve(request)
    assert result.request.prompt == "studio portrait new subject"
    assert result.request.guidance == Decimal("3.125")
    assert result.request.width == 512
    assert result.request.loras == []
    assert result.request.content_mode == ImageContentMode.EXPLICIT
    assert result.dependency_ids == frozenset({MODEL, LORA})
    assert result.required_entitlements == frozenset({"explicit", "base", "adapter"})
    assert result.request.preset_id is None
    assert result.preset_id == PRESET_ID
    assert result.preset_revision == 3


def test_prefix_is_not_duplicated_on_prefixed_override() -> None:
    request = ImageSubmitRequest(preset_id=PRESET_ID, prompt="studio portrait new subject")
    assert _resolve(request).request.prompt == "studio portrait new subject"


@pytest.mark.parametrize(
    "published, submit",
    [
        (False, ImageSubmitRequest(preset_id=PRESET_ID)),
        (True, ImageSubmitRequest(preset_id=PRESET_ID, preset_revision=2)),
        (True, ImageSubmitRequest(preset_id=PRESET_ID, model_id=uuid.uuid4())),
    ],
)
def test_unpublished_stale_or_rebound_preset_is_refused(
    published: bool, submit: ImageSubmitRequest
) -> None:
    with pytest.raises(ImageConflict):
        _resolve(submit, published=published)


def test_retired_preset_and_missing_registry_dependency_fail_closed() -> None:
    request = ImageSubmitRequest(preset_id=PRESET_ID)
    with pytest.raises(ImageConflict):
        resolve_image_preset(
            request,
            _preset(retired=True),
            published=True,
            preset_dependency_ids=frozenset({LORA}),
            preset_requirements=frozenset(),
            registry_requirements={MODEL: frozenset()},
        )
    with pytest.raises(ImageValidationError):
        _resolve(request, registry_requirements={MODEL: frozenset()})


def test_overlong_prefixed_prompt_is_rejected() -> None:
    with pytest.raises(ImageValidationError):
        _resolve(ImageSubmitRequest(preset_id=PRESET_ID, prompt="x" * 3990))
