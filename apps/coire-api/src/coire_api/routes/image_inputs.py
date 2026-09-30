"""Owner-scoped bounded image input upload route; recipe purpose only for now."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile, status
from pydantic import ValidationError

from coire_api.deps import SessionDep
from coire_api.images import input_deletion, inputs
from coire_api.images.authorization import CurrentImageUser, require_owned_image_input
from coire_api.images.telemetry import (
    ImageOperation,
    ImageOutcome,
    ImageReason,
    image_span,
    record_image_request,
)
from coire_core.errors import (
    CoireError,
    ImageForbidden,
    ImageInputTooLarge,
    ImageNotFound,
    ImageQuotaExceeded,
    ImageUnsupportedInput,
)
from coire_core.models.images import ImageInput, ImageInputUpload
from coire_core.settings import get_settings

router = APIRouter(prefix="/api/v1/image-inputs", tags=["images"])


@router.delete("/{input_id}", response_model=ImageInput, status_code=status.HTTP_202_ACCEPTED)
async def delete_image_input(
    input_id: uuid.UUID, principal: CurrentImageUser, session: SessionDep, response: Response
) -> ImageInput:
    with image_span(ImageOperation.DELETE):
        try:
            result = await input_deletion.tombstone_owned_input(session, principal, input_id)
            await session.commit()
        except ImageNotFound:
            record_image_request(
                ImageOperation.DELETE, ImageOutcome.REFUSED, reason=ImageReason.AUTH
            )
            raise
        except CoireError:
            record_image_request(
                ImageOperation.DELETE, ImageOutcome.REFUSED, reason=ImageReason.DEPENDENCY
            )
            raise
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.DELETE, ImageOutcome.ACCEPTED)
        return result


@router.get("/{input_id}", response_model=ImageInput)
async def get_image_input(
    input_id: uuid.UUID, principal: CurrentImageUser, session: SessionDep, response: Response
) -> ImageInput:
    with image_span(ImageOperation.INPUT_UPLOAD):
        row = await require_owned_image_input(session, input_id, principal)
        result = inputs.project_image_input(row)
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.INPUT_UPLOAD, ImageOutcome.SUCCEEDED)
        return result


@router.post("", response_model=ImageInput, status_code=status.HTTP_202_ACCEPTED)
async def upload_image_input(
    request: Request,
    response: Response,
    purpose: Annotated[str, Form()],
    filename: Annotated[str, Form()],
    byte_count: Annotated[int, Form()],
    file: Annotated[UploadFile, File()],
    principal: CurrentImageUser,
    session: SessionDep,
) -> ImageInput:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    with image_span(ImageOperation.INPUT_UPLOAD):
        if not settings.image_enabled:
            record_image_request(
                ImageOperation.INPUT_UPLOAD, ImageOutcome.REFUSED, reason=ImageReason.DEPENDENCY
            )
            raise ImageForbidden()
        try:
            metadata = ImageInputUpload.model_validate(
                {"purpose": purpose, "filename": filename, "byte_count": byte_count}
            )
        except ValidationError as exc:
            record_image_request(
                ImageOperation.INPUT_UPLOAD, ImageOutcome.REFUSED, reason=ImageReason.FORMAT
            )
            raise HTTPException(status_code=422, detail="invalid image upload metadata") from exc
        if metadata.purpose != "recipe":
            record_image_request(
                ImageOperation.INPUT_UPLOAD, ImageOutcome.REFUSED, reason=ImageReason.FORMAT
            )
            raise ImageUnsupportedInput()
        assert principal.user_id is not None
        try:
            result = await inputs.admit_recipe_upload(
                session, principal.user_id, metadata, file, settings
            )
        except ImageInputTooLarge:
            record_image_request(
                ImageOperation.INPUT_UPLOAD, ImageOutcome.REFUSED, reason=ImageReason.SIZE
            )
            raise
        except ImageQuotaExceeded:
            record_image_request(
                ImageOperation.INPUT_UPLOAD, ImageOutcome.REFUSED, reason=ImageReason.QUOTA
            )
            raise
        except CoireError:
            record_image_request(
                ImageOperation.INPUT_UPLOAD, ImageOutcome.FAILED, reason=ImageReason.STORAGE
            )
            raise
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.INPUT_UPLOAD, ImageOutcome.ACCEPTED)
        return result
