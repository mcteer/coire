"""Authenticated Studio image job commands."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from coire_core.models.image_worker import NodeImageJob, NodeImageStartRequest
from coire_node.deps import ImageDispatcherDep
from coire_node.image_dispatch import ImageDispatchConflict, ImageDispatchUnavailable
from coire_node.image_jobs import ImageJournalConflict, ImageJournalUnavailable

router = APIRouter(prefix="/node/images/jobs", tags=["image-jobs"])


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
