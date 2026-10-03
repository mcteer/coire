"""Synchronous compatible image generation over the native admission path."""

from __future__ import annotations

import asyncio
import base64
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import session_scope
from coire_api.images import admission, downloads, jobs
from coire_core.errors import ImageGenerationFailed, ImageTimeout, ImageValidationError
from coire_core.models.images import (
    ImageJob,
    ImageJobState,
    ImageSubmitRequest,
    OpenAIImageData,
    OpenAIImageGenerationRequest,
    OpenAIImageGenerationResponse,
)
from coire_core.settings import Settings

_TERMINAL = frozenset({ImageJobState.SUCCEEDED, ImageJobState.FAILED, ImageJobState.CANCELLED})
Sleep = Callable[[float], Awaitable[None]]


def native_submit(request: OpenAIImageGenerationRequest) -> ImageSubmitRequest:
    """Map supported compatible fields once. Quality has no profile mapping yet."""
    if request.quality is not None:
        raise ImageValidationError("quality has no model profile mapping")
    width = height = None
    if request.size is not None:
        width_text, height_text = request.size.split("x", 1)
        width, height = int(width_text), int(height_text)
    return ImageSubmitRequest(
        model_id=request.model,
        preset_id=request.coire_preset_id,
        preset_revision=request.coire_preset_revision,
        prompt=request.prompt,
        width=width,
        height=height,
        seed=request.coire_seed,
        n=request.n,
        content_mode=request.coire_content_mode,
    )


def fresh_idempotency_key() -> str:
    return uuid.uuid4().hex


async def await_image_generation(
    principal: Principal,
    job_id: str,
    settings: Settings,
    request: Request,
    *,
    sleep: Sleep = asyncio.sleep,
    now: Callable[[], float] = time.monotonic,
) -> ImageJob:
    """Poll the committed job. Disconnect and timeout leave the job running."""
    deadline = now() + settings.image_compatible_wait_s
    while True:
        if await request.is_disconnected():
            raise ImageTimeout(job_id)
        async with session_scope() as session:
            job = await jobs.get_owned_image_job(session, principal, job_id)
        if job.state in _TERMINAL:
            return job
        remaining = deadline - now()
        if remaining <= 0:
            raise ImageTimeout(job_id)
        await sleep(min(1.0, remaining))


async def render_generation(
    session: AsyncSession,
    principal: Principal,
    job: ImageJob,
    response_format: str,
    blob_root: str,
) -> OpenAIImageGenerationResponse:
    """Return authenticated fragment URLs or the same PNG bytes native downloads serve."""
    if job.state is ImageJobState.CANCELLED:
        raise ImageGenerationFailed(job.id, job.failure_code or "cancelled")
    if job.state is not ImageJobState.SUCCEEDED or not job.outputs:
        raise ImageGenerationFailed(job.id, job.failure_code or "image_failed")
    data: list[OpenAIImageData] = []
    for output in sorted(job.outputs, key=lambda item: item.index):
        grant = await downloads.issue_download_grant(session, principal, output.id)
        if response_format == "url":
            data.append(OpenAIImageData(url=grant.url))
            continue
        token = grant.url.rsplit("#grant=", 1)[-1]
        row = await downloads.redeem_download_grant(session, principal, output.id, token)
        fd = downloads.open_verified_blob(Path(blob_root), row)
        try:
            raw = await asyncio.to_thread(os.read, fd, row.size_bytes)
        finally:
            os.close(fd)
        data.append(OpenAIImageData(b64_json=base64.b64encode(raw).decode("ascii")))
    await session.commit()
    return OpenAIImageGenerationResponse(
        created=int(job.updated_at.timestamp()),
        data=data,
        coire_job_id=job.id,
    )


async def admit_compatible_job(
    session: AsyncSession,
    principal: Principal,
    body: OpenAIImageGenerationRequest,
    idempotency_key: str | None,
    settings: Settings,
) -> str:
    """Refuse unsupported fields before admission so no job or quota hold is created."""
    native = native_submit(body)
    receipt = await admission.admit_image_job(
        session,
        principal,
        native,
        idempotency_key or fresh_idempotency_key(),
        settings,
    )
    return receipt.job_id
