"""Private image API."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, Request, Response
from fastapi.responses import StreamingResponse

from coire_api.db import session_scope
from coire_api.deps import SessionDep
from coire_api.images import admission, cancellation, catalog, events, jobs, presets
from coire_api.images.authorization import CurrentImageUser
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
    ImageNotFound,
    ImageQuotaExceeded,
    ImageValidationError,
)
from coire_core.models.files import ULID_PATTERN
from coire_core.models.images import (
    ImageGenerationLimits,
    ImageJob,
    ImageJobEvent,
    ImageJobPage,
    ImageJobReceipt,
    ImageJobState,
    ImageModelList,
    ImagePresetList,
    ImageSubmitRequest,
)
from coire_core.settings import get_settings

router = APIRouter(prefix="/api/v1/images", tags=["images"])
_EVENT_POLL_SECONDS = 1.0
_EVENT_HEARTBEAT_SECONDS = 15.0


@router.get("/presets", response_model=ImagePresetList)
async def list_presets(
    request: Request, principal: CurrentImageUser, session: SessionDep, response: Response
) -> ImagePresetList:
    """Show only currently usable published presets; no job is admitted here."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    with image_span(ImageOperation.PRESET_LIST):
        if not settings.image_enabled:
            response.headers["Cache-Control"] = "private, no-store"
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
        response.headers["Cache-Control"] = "private, no-store"
        return result


@router.get("/models", response_model=ImageModelList)
async def list_image_models(
    request: Request, principal: CurrentImageUser, session: SessionDep, response: Response
) -> ImageModelList:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    limits = ImageGenerationLimits(
        generation_input_max_bytes=settings.image_generation_input_max_bytes,
        recipe_input_max_bytes=settings.image_recipe_input_max_bytes,
        output_max_bytes=settings.image_output_max_bytes,
        owner_storage_quota_bytes=settings.image_owner_storage_quota_bytes,
        pending_per_owner=settings.image_pending_per_owner,
        daily_outputs_per_owner=settings.image_daily_outputs_per_owner,
        output_retention_hours=settings.image_output_retention_hours,
    )
    with image_span(ImageOperation.MODEL_LIST):
        if not settings.image_enabled:
            response.headers["Cache-Control"] = "private, no-store"
            record_image_request(ImageOperation.MODEL_LIST, ImageOutcome.ACCEPTED)
            return ImageModelList(items=[], limits=limits)
        try:
            result = await catalog.list_eligible_image_models(session, principal)
        except ImageForbidden:
            record_image_request(
                ImageOperation.MODEL_LIST, ImageOutcome.REFUSED, reason=ImageReason.AUTH
            )
            raise
        except CoireError:
            record_image_request(
                ImageOperation.MODEL_LIST, ImageOutcome.FAILED, reason=ImageReason.INTERNAL
            )
            raise
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.MODEL_LIST, ImageOutcome.SUCCEEDED)
        return result.model_copy(update={"limits": limits})


@router.post("", response_model=ImageJobReceipt, status_code=202)
async def submit_image_job(
    request: Request,
    body: ImageSubmitRequest,
    principal: CurrentImageUser,
    session: SessionDep,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", max_length=128)],
) -> ImageJobReceipt:
    """Persist the job before returning its receipt. Retries with the same key replay it."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    with image_span(ImageOperation.SUBMIT):
        try:
            result = await admission.admit_image_job(
                session, principal, body, idempotency_key, settings
            )
        except ImageForbidden:
            record_image_request(
                ImageOperation.SUBMIT, ImageOutcome.REFUSED, reason=ImageReason.AUTH
            )
            raise
        except ImageQuotaExceeded:
            record_image_request(
                ImageOperation.SUBMIT, ImageOutcome.REFUSED, reason=ImageReason.QUOTA
            )
            raise
        except ImageConflict:
            record_image_request(
                ImageOperation.SUBMIT, ImageOutcome.FAILED, reason=ImageReason.CONFLICT
            )
            raise
        except ImageValidationError:
            record_image_request(
                ImageOperation.SUBMIT, ImageOutcome.FAILED, reason=ImageReason.FORMAT
            )
            raise
        except CoireError:
            record_image_request(
                ImageOperation.SUBMIT, ImageOutcome.FAILED, reason=ImageReason.INTERNAL
            )
            raise
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.SUBMIT, ImageOutcome.ACCEPTED)
        return result


@router.get("", response_model=ImageJobPage)
async def list_image_jobs(
    principal: CurrentImageUser,
    session: SessionDep,
    response: Response,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=100),
    state: ImageJobState | None = None,
) -> ImageJobPage:
    with image_span(ImageOperation.JOB_READ):
        try:
            result = await jobs.list_owned_image_jobs(
                session, principal, limit=limit, cursor=cursor, state=state
            )
        except (ImageForbidden, ImageNotFound):
            record_image_request(
                ImageOperation.JOB_READ, ImageOutcome.REFUSED, reason=ImageReason.AUTH
            )
            raise
        except CoireError:
            record_image_request(
                ImageOperation.JOB_READ, ImageOutcome.FAILED, reason=ImageReason.INTERNAL
            )
            raise
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.JOB_READ, ImageOutcome.SUCCEEDED)
        return result


@router.delete("/{job_id}", response_model=ImageJob, status_code=202)
async def cancel_image_job(
    principal: CurrentImageUser,
    session: SessionDep,
    response: Response,
    job_id: str = Path(pattern=ULID_PATTERN),
) -> ImageJob:
    with image_span(ImageOperation.CANCEL, job_id=job_id):
        try:
            result, terminal = await cancellation.request_image_job_cancel(
                session, principal, job_id
            )
        except (ImageForbidden, ImageNotFound):
            record_image_request(
                ImageOperation.CANCEL, ImageOutcome.REFUSED, reason=ImageReason.AUTH, job_id=job_id
            )
            raise
        except CoireError:
            record_image_request(
                ImageOperation.CANCEL,
                ImageOutcome.FAILED,
                reason=ImageReason.INTERNAL,
                job_id=job_id,
            )
            raise
        response.status_code = 200 if terminal else 202
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(
            ImageOperation.CANCEL,
            ImageOutcome.CANCELLED if terminal else ImageOutcome.ACCEPTED,
            job_id=job_id,
        )
        return result


@router.get("/{job_id}", response_model=ImageJob)
async def get_image_job(
    principal: CurrentImageUser,
    session: SessionDep,
    response: Response,
    job_id: str = Path(pattern=ULID_PATTERN),
) -> ImageJob:
    with image_span(ImageOperation.JOB_READ, job_id=job_id):
        try:
            result = await jobs.get_owned_image_job(session, principal, job_id)
        except (ImageForbidden, ImageNotFound):
            record_image_request(
                ImageOperation.JOB_READ,
                ImageOutcome.REFUSED,
                reason=ImageReason.AUTH,
                job_id=job_id,
            )
            raise
        except CoireError:
            record_image_request(
                ImageOperation.JOB_READ,
                ImageOutcome.FAILED,
                reason=ImageReason.INTERNAL,
                job_id=job_id,
            )
            raise
        response.headers["Cache-Control"] = "private, no-store"
        record_image_request(ImageOperation.JOB_READ, ImageOutcome.SUCCEEDED, job_id=job_id)
        return result


@router.get(
    "/{job_id}/events",
    response_class=StreamingResponse,
    responses={
        200: {
            "model": ImageJobEvent,
            "description": "SSE frames; each data payload is an ImageJobEvent",
        }
    },
)
async def image_job_events(
    request: Request,
    principal: CurrentImageUser,
    session: SessionDep,
    job_id: str = Path(pattern=ULID_PATTERN),
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Replay durable events; recheck owner and entitlements at every poll."""
    cursor = events.parse_event_cursor(last_event_id, job_id)
    with image_span(ImageOperation.JOB_EVENTS, job_id=job_id):
        await jobs.get_owned_image_job(session, principal, job_id)

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        last_sent = time.monotonic()
        while not await request.is_disconnected():
            try:
                async with session_scope() as event_session:
                    with image_span(ImageOperation.JOB_EVENTS, job_id=job_id):
                        snapshot, pending = await events.read_owned_image_events(
                            event_session, principal, job_id, cursor
                        )
            except (ImageForbidden, ImageNotFound):
                record_image_request(
                    ImageOperation.JOB_EVENTS,
                    ImageOutcome.REFUSED,
                    reason=ImageReason.AUTH,
                    job_id=job_id,
                )
                return
            for event in pending:
                cursor = event.sequence
                yield events.encode_event(event)
                last_sent = time.monotonic()
            if events.is_terminal(snapshot) and cursor >= snapshot.latest_event_sequence:
                record_image_request(
                    ImageOperation.JOB_EVENTS, ImageOutcome.SUCCEEDED, job_id=job_id
                )
                return
            if len(pending) == 100:
                continue
            if time.monotonic() - last_sent >= _EVENT_HEARTBEAT_SECONDS:
                yield ": keep-alive\n\n"
                last_sent = time.monotonic()
            await asyncio.sleep(_EVENT_POLL_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
    )
