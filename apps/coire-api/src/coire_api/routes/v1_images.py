"""Bounded synchronous `/v1/images/generations` adapter."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from coire_api.db import session_scope
from coire_api.deps import SessionDep
from coire_api.images.authorization import CurrentImageUser
from coire_api.images.compatible import (
    admit_compatible_job,
    await_image_generation,
    render_generation,
)
from coire_api.images.telemetry import (
    ImageOperation,
    ImageOutcome,
    ImageReason,
    image_span,
    record_image_request,
)
from coire_core.errors import (
    CoireError,
    ImageConflict,
    ImageForbidden,
    ImageGenerationFailed,
    ImageQuotaExceeded,
    ImageTimeout,
    ImageValidationError,
)
from coire_core.models.images import (
    ImageJobState,
    OpenAIImageGenerationRequest,
    OpenAIImageGenerationResponse,
)
from coire_core.settings import get_settings

router = APIRouter(prefix="/v1/images", tags=["compatible"])


def _reason(exc: CoireError) -> ImageReason:
    if isinstance(exc, ImageValidationError):
        return ImageReason.FORMAT
    if isinstance(exc, ImageConflict):
        return ImageReason.CONFLICT
    if isinstance(exc, ImageQuotaExceeded):
        return ImageReason.QUOTA
    return ImageReason.INTERNAL


def _problem(request: Request, exc: CoireError, job_id: str | None = None) -> JSONResponse:
    payload = (
        exc.to_problem()
        .model_copy(update={"instance": request.url.path})
        .model_dump(mode="json", exclude_none=True)
    )
    if job_id is not None:
        payload["coire_job_id"] = job_id
    record_image_request(
        ImageOperation.COMPATIBLE,
        ImageOutcome.FAILED,
        reason=_reason(exc),
        job_id=job_id,
    )
    return JSONResponse(
        status_code=exc.status, media_type="application/problem+json", content=payload
    )


@router.post("/generations", response_model=OpenAIImageGenerationResponse)
async def generate_image(
    request: Request,
    body: OpenAIImageGenerationRequest,
    principal: CurrentImageUser,
    session: SessionDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> OpenAIImageGenerationResponse | JSONResponse:
    """Wait for the native job. A timeout keeps the accepted job and returns its id."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    with image_span(ImageOperation.COMPATIBLE):
        try:
            job_id = await admit_compatible_job(session, principal, body, idempotency_key, settings)
        except ImageForbidden:
            record_image_request(
                ImageOperation.COMPATIBLE, ImageOutcome.REFUSED, reason=ImageReason.AUTH
            )
            raise
        except ImageQuotaExceeded:
            record_image_request(
                ImageOperation.COMPATIBLE, ImageOutcome.REFUSED, reason=ImageReason.QUOTA
            )
            raise
        except ImageValidationError as exc:
            return _problem(request, exc)
        except (ImageConflict, CoireError) as exc:
            return _problem(request, exc)
        try:
            job = await await_image_generation(principal, job_id, settings, request)
        except ImageTimeout as exc:
            return _problem(request, exc, exc.job_id)
        if job.state is not ImageJobState.SUCCEEDED:
            failed = ImageGenerationFailed(job.id, job.failure_code or job.state.value)
            return _problem(request, failed, job.id)
        try:
            async with session_scope() as render_session:
                result = await render_generation(
                    render_session,
                    principal,
                    job,
                    body.response_format,
                    settings.image_blob_root,
                )
        except (ImageGenerationFailed, CoireError) as exc:
            job_ref = getattr(exc, "job_id", job_id)
            return _problem(request, exc, job_ref if isinstance(job_ref, str) else job_id)
    response.headers["Cache-Control"] = "private, no-store"
    record_image_request(ImageOperation.COMPATIBLE, ImageOutcome.SUCCEEDED, job_id=job_id)
    return result
