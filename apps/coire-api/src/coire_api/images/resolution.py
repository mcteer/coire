"""Strict basic image settings resolution before job admission."""

from __future__ import annotations

import secrets
from collections.abc import Callable

from pydantic import ValidationError

from coire_core.errors import ImageValidationError
from coire_core.models.images import (
    ImageCapabilityProfile,
    ImageContentMode,
    ImageMode,
    ImageSpec,
    ImageSubmitRequest,
)


def resolve_basic_image_spec(
    request: ImageSubmitRequest,
    profile: ImageCapabilityProfile,
    *,
    random_seed: Callable[[], int] | None = None,
) -> ImageSpec:
    """Freeze basic txt2img settings; caller validates registry and owner first."""
    if (
        request.model_id is None
        or request.mode not in (None, ImageMode.TXT2IMG, ImageMode.IMG2IMG, ImageMode.CONTROL)
        or request.variant_id is not None
        or request.mask_id is not None
    ):
        raise ImageValidationError("unsupported image setting")
    if (
        profile.default_width is None
        or profile.default_height is None
        or profile.default_steps is None
        or profile.default_guidance is None
    ):
        raise ImageValidationError("image model defaults unavailable")
    values = {
        "model_id": request.model_id,
        "mode": request.mode or ImageMode.TXT2IMG,
        "prompt": request.prompt,
        "negative_prompt": request.negative_prompt,
        "width": request.width if request.width is not None else profile.default_width,
        "height": request.height if request.height is not None else profile.default_height,
        "steps": request.steps if request.steps is not None else profile.default_steps,
        "guidance": request.guidance if request.guidance is not None else profile.default_guidance,
        "seed": request.seed
        if request.seed is not None
        else (random_seed or (lambda: secrets.randbits(32)))(),
        "n": request.n if request.n is not None else 1,
        "init_image_id": request.init_image_id,
        "strength": request.strength,
        "loras": request.loras or [],
        "upscale": request.upscale,
        "control": request.control,
        "output": request.output,
        "content_mode": request.content_mode or ImageContentMode.STANDARD,
    }
    if values["output"] is None:
        values.pop("output")
    try:
        spec = ImageSpec.model_validate(values)
        profile.validate_spec(spec)
    except ValidationError as exc:
        field = str(exc.errors()[0]["loc"][0]) if exc.errors()[0]["loc"] else "spec"
        raise ImageValidationError(f"invalid {field} setting") from exc
    except ValueError as exc:
        raise ImageValidationError(str(exc)) from exc
    if spec.guidance != 0 or spec.negative_prompt is not None:
        raise ImageValidationError("unsupported image setting")
    if (
        any(item.variant_id is not None for item in spec.loras)
        or (spec.control is not None and spec.control.variant_id is not None)
        or (spec.upscale is not None and spec.upscale.variant_id is not None)
    ):
        raise ImageValidationError("image auxiliary variants are unsupported")
    if spec.mode is ImageMode.CONTROL and spec.loras:
        raise ImageValidationError("control mode does not support a LoRA stack")
    return spec
