"""Private image output metadata routes."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from coire_api.deps import SessionDep
from coire_api.images import deletion, downloads, outputs
from coire_api.images.authorization import CurrentImageUser
from coire_api.images.telemetry import (
    ImageOperation,
    ImageOutcome,
    ImageReason,
    image_span,
    record_image_request,
)
from coire_core.errors import CoireError, ImageForbidden, ImageNotFound
from coire_core.models.images import (
    ImageContentTag,
    ImageDeletionReceipt,
    ImageDownloadGrant,
    ImageOutput,
    ImageOutputPage,
)
from coire_core.settings import get_settings

router = APIRouter(prefix="/api/v1/image-outputs", tags=["images"])


@router.delete(
    "/{output_id}",
    response_model=ImageDeletionReceipt,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_output(
    output_id: uuid.UUID,
    principal: CurrentImageUser,
    session: SessionDep,
    response: Response,
) -> ImageDeletionReceipt:
    with image_span(ImageOperation.DELETE):
        try:
            receipt = await deletion.tombstone_owned_output(session, principal, output_id)
            await session.commit()
        except ImageNotFound:
            record_image_request(
                ImageOperation.DELETE, ImageOutcome.REFUSED, reason=ImageReason.AUTH
            )
            raise
        except CoireError:
            record_image_request(
                ImageOperation.DELETE, ImageOutcome.FAILED, reason=ImageReason.INTERNAL
            )
            raise
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.DELETE, ImageOutcome.ACCEPTED)
        return receipt


@router.get("", response_model=ImageOutputPage)
async def list_outputs(
    principal: CurrentImageUser,
    session: SessionDep,
    response: Response,
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
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.GALLERY, ImageOutcome.SUCCEEDED)
        return result


@router.get("/{output_id}", response_model=ImageOutput)
async def get_output(
    output_id: uuid.UUID, principal: CurrentImageUser, session: SessionDep, response: Response
) -> ImageOutput:
    with image_span(ImageOperation.GALLERY):
        try:
            result = await outputs.get_owned_output(session, principal, output_id)
        except (ImageForbidden, ImageNotFound):
            record_image_request(
                ImageOperation.GALLERY, ImageOutcome.REFUSED, reason=ImageReason.AUTH
            )
            raise
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.GALLERY, ImageOutcome.SUCCEEDED)
        return result


@router.post("/{output_id}/download-grants", response_model=ImageDownloadGrant)
async def create_download_grant(
    output_id: uuid.UUID,
    principal: CurrentImageUser,
    session: SessionDep,
    response: Response,
) -> ImageDownloadGrant:
    with image_span(ImageOperation.DOWNLOAD):
        try:
            grant = await downloads.issue_download_grant(session, principal, output_id)
            await session.commit()
        except (ImageForbidden, ImageNotFound):
            record_image_request(
                ImageOperation.DOWNLOAD, ImageOutcome.REFUSED, reason=ImageReason.AUTH
            )
            raise
        except CoireError:
            record_image_request(
                ImageOperation.DOWNLOAD, ImageOutcome.FAILED, reason=ImageReason.INTERNAL
            )
            raise
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        record_image_request(ImageOperation.DOWNLOAD, ImageOutcome.ACCEPTED)
        return grant


@router.get(
    "/{output_id}/content",
    response_class=StreamingResponse,
    responses={200: {"content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}},
)
async def download_content(
    output_id: uuid.UUID,
    request: Request,
    principal: CurrentImageUser,
    session: SessionDep,
    grant: str = Header(alias="X-Coire-Image-Grant", min_length=1, max_length=64),
) -> StreamingResponse:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    with image_span(ImageOperation.DOWNLOAD):
        try:
            row = await downloads.redeem_download_grant(session, principal, output_id, grant)
            fd = downloads.open_verified_blob(Path(settings.image_blob_root), row)
        except (ImageForbidden, ImageNotFound):
            record_image_request(
                ImageOperation.DOWNLOAD, ImageOutcome.REFUSED, reason=ImageReason.AUTH
            )
            raise
        except CoireError:
            record_image_request(
                ImageOperation.DOWNLOAD, ImageOutcome.FAILED, reason=ImageReason.STORAGE
            )
            raise

    async def chunks() -> AsyncIterator[bytes]:
        try:
            with image_span(ImageOperation.DOWNLOAD):
                while block := await asyncio.to_thread(os.read, fd, 256 * 1024):
                    yield block
            record_image_request(ImageOperation.DOWNLOAD, ImageOutcome.SUCCEEDED)
        except asyncio.CancelledError:
            record_image_request(ImageOperation.DOWNLOAD, ImageOutcome.CANCELLED)
            raise
        except OSError:
            record_image_request(
                ImageOperation.DOWNLOAD, ImageOutcome.FAILED, reason=ImageReason.STORAGE
            )
            raise
        finally:
            os.close(fd)

    return StreamingResponse(
        chunks(),
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(row.size_bytes),
            "Content-Disposition": f'attachment; filename="coire-{row.id}.png"',
        },
    )
