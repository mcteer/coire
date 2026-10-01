"""Authenticated Studio image job commands."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, status

from coire_core.models.files import ULID_PATTERN
from coire_core.models.image_worker import (
    ImageJobBinding,
    NodeImageCancelRequest,
    NodeImageCleanupReceipt,
    NodeImageCleanupRequest,
    NodeImageInputReceipt,
    NodeImageInputRequest,
    NodeImageJob,
    NodeImageStartRequest,
    NodeImageTransferRequest,
)
from coire_node.deps import ImageDispatcherDep
from coire_node.image_cleanup import ImageCleanupUnavailable
from coire_node.image_dispatch import ImageDispatchConflict, ImageDispatchUnavailable
from coire_node.image_jobs import ImageJournalConflict, ImageJournalUnavailable
from coire_node.image_transfer import ImageTransferUnavailable

router = APIRouter(prefix="/node/images/jobs", tags=["image-jobs"])


@router.put("/{job_id}/reserve-inputs", response_model=NodeImageJob)
async def reserve_image_inputs(
    job_id: str, request: NodeImageStartRequest, response: Response, dispatcher: ImageDispatcherDep
) -> NodeImageJob:
    if job_id != request.job_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "image job differs from request")
    try:
        created, item = await dispatcher.reserve_inputs(request)
    except (ImageJournalConflict, ImageDispatchConflict):
        raise HTTPException(status.HTTP_409_CONFLICT, "image job binding unavailable") from None
    except (ImageJournalUnavailable, ImageDispatchUnavailable):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "image job unavailable") from None
    response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    return item


@router.put("/{job_id}/inputs/{input_id}", response_model=NodeImageInputReceipt)
async def stage_image_input(
    job_id: str,
    input_id: uuid.UUID,
    request: Request,
    dispatcher: ImageDispatcherDep,
    attempt: int = Query(ge=1),
    fence: int = Query(ge=1),
    purpose: Literal["init", "mask", "control"] = Query(),
    sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    byte_count: int = Query(ge=1, le=10 * 1024 * 1024),
) -> NodeImageInputReceipt:
    command = NodeImageInputRequest(
        job_id=job_id,
        attempt=attempt,
        fence=fence,
        node=dispatcher.journal.node,
        input_id=input_id,
        purpose=purpose,
        sha256=sha256,
        byte_count=byte_count,
    )
    try:
        return await dispatcher.stage_input(command, request.stream())
    except (ImageJournalConflict, ImageDispatchConflict):
        raise HTTPException(status.HTTP_409_CONFLICT, "image input binding unavailable") from None
    except (ImageJournalUnavailable, ImageDispatchUnavailable):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "image input unavailable"
        ) from None


@router.put("/{job_id}", response_model=NodeImageJob)
async def start_image_job(
    job_id: str, request: NodeImageStartRequest, response: Response, dispatcher: ImageDispatcherDep
) -> NodeImageJob:
    if job_id != request.job_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "image job differs from request")
    try:
        created, item = await dispatcher.start(request)
    except (ImageJournalConflict, ImageDispatchConflict):
        raise HTTPException(status.HTTP_409_CONFLICT, "image job binding unavailable") from None
    except (ImageJournalUnavailable, ImageDispatchUnavailable):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "image job unavailable") from None
    response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    return item


@router.get("/{job_id}", response_model=NodeImageJob)
async def get_image_job(
    dispatcher: ImageDispatcherDep,
    job_id: str = Path(pattern=ULID_PATTERN),
    attempt: int = Query(ge=1),
    fence: int = Query(ge=1),
) -> NodeImageJob:
    binding = ImageJobBinding(job_id=job_id, attempt=attempt, fence=fence)
    try:
        item = await dispatcher.status(binding)
    except ImageDispatchConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, "image job binding unavailable") from None
    except (ImageJournalUnavailable, ImageDispatchUnavailable):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "image job unavailable") from None
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "image job unavailable")
    return item


@router.delete("/{job_id}", response_model=NodeImageJob)
async def cancel_image_job(
    job_id: str, request: NodeImageCancelRequest, dispatcher: ImageDispatcherDep
) -> NodeImageJob:
    if job_id != request.job_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "image job differs from request")
    try:
        item = await dispatcher.cancel(request)
    except (ImageJournalConflict, ImageDispatchConflict):
        raise HTTPException(status.HTTP_409_CONFLICT, "image job binding unavailable") from None
    except (ImageJournalUnavailable, ImageDispatchUnavailable, ImageCleanupUnavailable):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "image job unavailable") from None
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "image job unavailable")
    return item


@router.post("/{job_id}/cleanup", response_model=NodeImageCleanupReceipt)
async def cleanup_image_job(
    job_id: str, request: NodeImageCleanupRequest, dispatcher: ImageDispatcherDep
) -> NodeImageCleanupReceipt:
    if job_id != request.job_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "image job differs from request")
    try:
        return await dispatcher.cleanup(request)
    except ImageJournalConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, "image cleanup binding unavailable") from None
    except (ImageJournalUnavailable, ImageCleanupUnavailable):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "image cleanup unavailable"
        ) from None


@router.post("/{job_id}/transfer", response_model=NodeImageJob)
async def transfer_image_job(
    job_id: str, request: NodeImageTransferRequest, dispatcher: ImageDispatcherDep
) -> NodeImageJob:
    if job_id != request.job_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "image job differs from request")
    try:
        return await dispatcher.transfer(request)
    except ImageJournalConflict:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "image transfer binding unavailable"
        ) from None
    except (ImageJournalUnavailable, ImageTransferUnavailable, ImageCleanupUnavailable):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "image transfer unavailable"
        ) from None
