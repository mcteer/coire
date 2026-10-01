"""Private node-authenticated output transfer; no human or service credential is accepted."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Request, Response, status
from pydantic import ValidationError

from coire_api.deps import SessionDep, SettingsDep
from coire_api.images.telemetry import (
    ImageOperation,
    ImageOutcome,
    ImageReason,
    image_span,
    record_image_request,
)
from coire_api.images.transfer import receive_transfer, require_node_credential
from coire_core.errors import ImageConflict
from coire_core.models.files import ULID_PATTERN
from coire_core.models.image_worker import ImageTransferReceipt, ImageTransferUploadRequest

router = APIRouter(prefix="/api/v1/internal/images", tags=["internal: images"])


@router.put("/{job_id}/outputs/{index}", response_model=ImageTransferReceipt)
async def upload_image_output(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    job_id: str = Path(pattern=ULID_PATTERN),
    index: int = Path(ge=0, le=3),
    node: Annotated[str | None, Header(alias="x-coire-node")] = None,
    attempt: Annotated[int | None, Header(alias="x-coire-attempt")] = None,
    fence: Annotated[int | None, Header(alias="x-coire-fence")] = None,
    grant_token: Annotated[str | None, Header(alias="x-coire-transfer-grant")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> ImageTransferReceipt:
    """Stream a bounded PNG and issue a receipt after private storage is durable."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "node credential required")
    try:
        headers = ImageTransferUploadRequest.model_validate(
            {
                "job_id": job_id,
                "index": index,
                "node": node,
                "attempt": attempt,
                "fence": fence,
                "grant_token": grant_token,
            }
        )
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid transfer headers"
        ) from exc
    try:
        require_node_credential(headers.node, authorization.removeprefix("Bearer "), settings)
    except ImageConflict:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "node credential unavailable") from None
    with image_span(ImageOperation.TRANSFER, job_id=job_id):
        try:
            receipt, created = await receive_transfer(
                session,
                settings,
                job_id=job_id,
                index=index,
                attempt=headers.attempt,
                fence=headers.fence,
                node=headers.node,
                grant_token=headers.grant_token,
                body=request.stream(),
            )
            await session.commit()
        except ImageConflict:
            record_image_request(
                ImageOperation.TRANSFER,
                ImageOutcome.REFUSED,
                reason=ImageReason.CONFLICT,
                job_id=job_id,
            )
            raise
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        response.headers["Cache-Control"] = "no-store"
        record_image_request(ImageOperation.TRANSFER, ImageOutcome.SUCCEEDED, job_id=job_id)
        return receipt
