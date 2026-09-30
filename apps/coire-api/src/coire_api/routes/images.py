"""Private image API; admission stays disabled until worker and publication gates ship."""

from __future__ import annotations

from fastapi import APIRouter, Request

from coire_api.deps import SessionDep
from coire_api.images import presets
from coire_api.images.authorization import CurrentImageUser
from coire_api.images.telemetry import (
    ImageOperation,
    ImageOutcome,
    ImageReason,
    image_span,
    record_image_request,
)
from coire_core.errors import CoireError, ImageForbidden
from coire_core.models.images import ImagePresetList
from coire_core.settings import get_settings

router = APIRouter(prefix="/api/v1/images", tags=["images"])


@router.get("/presets", response_model=ImagePresetList)
async def list_presets(
    request: Request, principal: CurrentImageUser, session: SessionDep
) -> ImagePresetList:
    """Show only currently usable published presets; no job is admitted here."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    with image_span(ImageOperation.PRESET_LIST):
        if not settings.image_enabled:
            record_image_request(ImageOperation.PRESET_LIST, ImageOutcome.ACCEPTED)
            return ImagePresetList(items=[])
        try:
            result = await presets.list_eligible_image_presets(session, principal)
        except ImageForbidden:
            record_image_request(
                ImageOperation.PRESET_LIST, ImageOutcome.REFUSED, reason=ImageReason.AUTH
            )
            raise
        except CoireError:
            record_image_request(
                ImageOperation.PRESET_LIST, ImageOutcome.FAILED, reason=ImageReason.DEPENDENCY
            )
            raise
        record_image_request(ImageOperation.PRESET_LIST, ImageOutcome.ACCEPTED)
        return result
