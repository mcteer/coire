"""Private image output metadata routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from coire_api.deps import SessionDep
from coire_api.images import outputs
from coire_api.images.authorization import CurrentImageUser
from coire_api.images.telemetry import (
    ImageOperation,
    ImageOutcome,
    ImageReason,
    image_span,
    record_image_request,
)
from coire_core.errors import CoireError, ImageForbidden, ImageNotFound
from coire_core.models.images import ImageContentTag, ImageOutput, ImageOutputPage

router = APIRouter(prefix="/api/v1/image-outputs", tags=["images"])


@router.get("", response_model=ImageOutputPage)
async def list_outputs(
    principal: CurrentImageUser,
    session: SessionDep,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=100),
    tag: ImageContentTag | None = None,
) -> ImageOutputPage:
    with image_span(ImageOperation.GALLERY):
        try:
            result = await outputs.list_owned_outputs(
                session, principal, limit=limit, cursor=cursor, tag=tag
            )
        except (ImageForbidden, ImageNotFound):
            record_image_request(
                ImageOperation.GALLERY, ImageOutcome.REFUSED, reason=ImageReason.AUTH
            )
            raise
        except CoireError:
            record_image_request(
                ImageOperation.GALLERY, ImageOutcome.FAILED, reason=ImageReason.INTERNAL
            )
            raise
        record_image_request(ImageOperation.GALLERY, ImageOutcome.SUCCEEDED)
        return result


@router.get("/{output_id}", response_model=ImageOutput)
async def get_output(
    output_id: uuid.UUID, principal: CurrentImageUser, session: SessionDep
) -> ImageOutput:
    with image_span(ImageOperation.GALLERY):
        try:
            result = await outputs.get_owned_output(session, principal, output_id)
        except ImageNotFound:
            record_image_request(
                ImageOperation.GALLERY, ImageOutcome.REFUSED, reason=ImageReason.AUTH
            )
            raise
        record_image_request(ImageOperation.GALLERY, ImageOutcome.SUCCEEDED)
        return result
