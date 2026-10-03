"""Restart-safe DBOS handoff for owner recipe and normalized image inputs."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from dbos import DBOS
from opentelemetry import metrics, trace

from coire_api.db import ImageInputRow, session_scope
from coire_api.file_worker_client import FileWorkerClient, FileWorkerParseRefused
from coire_api.images.quota import _QUOTA_LOCK, settle_storage_hold
from coire_core.errors import ImageConflict
from coire_core.models.files import ImageFileProcessRequest, ImageFileProcessResult
from coire_core.models.image_worker import ImageRecipeParseRequest
from coire_core.settings import get_settings

tracer = trace.get_tracer("coire.scheduler.image_inputs")
processed_total = metrics.get_meter("coire.scheduler.image_inputs").create_counter(
    "coire_image_input_processing_total", unit="1", description="Image input processing outcomes"
)


def _verified_normalized(path: Path, result: ImageFileProcessResult) -> bool:
    """Read a same-volume private output before any ready transition."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size != result.normalized_bytes
            ):
                return False
            with os.fdopen(fd, "rb", closefd=False) as source:
                return hashlib.file_digest(source, "sha256").hexdigest() == result.normalized_sha256
        finally:
            os.close(fd)
    except OSError:
        return False


def _valid_processing_row(row: ImageInputRow) -> bool:
    return (
        row.state == "processing"
        and row.purpose == "recipe"
        and row.deleted_at is None
        and row.original_key == str(row.id)
        and row.original_sha256 is not None
        and row.processing_job_id is not None
        and row.held_bytes >= row.original_bytes > 0
    )


async def _fail_if_current(input_id: uuid.UUID, request: ImageRecipeParseRequest) -> None:
    async with session_scope() as session:
        row = await session.get(ImageInputRow, input_id, with_for_update=True)
        if (
            row is not None
            and row.state == "processing"
            and row.original_sha256 == request.source_sha256
            and row.original_bytes == request.byte_count
        ):
            row.state = "failed"
            row.updated_at = datetime.now(UTC)


async def drive_recipe_input(input_id: uuid.UUID) -> None:
    """Replay-safe parse; only the final locked transaction settles the hold."""
    async with session_scope() as session:
        row = await session.get(ImageInputRow, input_id)
        if row is None or row.state in {"ready", "failed", "deleting", "purged"}:
            return
        if not _valid_processing_row(row):
            row.state = "failed"
            row.updated_at = datetime.now(UTC)
            processed_total.add(1, {"outcome": "refused"})
            return
        assert row.original_sha256 is not None
        request = ImageRecipeParseRequest(
            input_id=row.id,
            source_sha256=row.original_sha256,
            byte_count=row.original_bytes,
        )
        owner_id = row.owner_user_id
        processing_job_id = row.processing_job_id

    with tracer.start_as_current_span("coire.scheduler.image_input.parse"):
        try:
            async with FileWorkerClient(get_settings()) as client:
                result = await client.parse_image_recipe(request)
        except FileWorkerParseRefused:
            await _fail_if_current(input_id, request)
            processed_total.add(1, {"outcome": "refused"})
            return

    async with session_scope() as session:
        # Match deletion/maintenance: quota lock before the input row.
        await session.execute(_QUOTA_LOCK)
        row = await session.get(ImageInputRow, input_id, with_for_update=True)
        if row is None or row.state != "processing":
            return
        if (
            not _valid_processing_row(row)
            or row.owner_user_id != owner_id
            or row.processing_job_id != processing_job_id
            or row.original_sha256 != request.source_sha256
            or row.original_bytes != request.byte_count
            or result.input_id != input_id
            or result.source_sha256 != request.source_sha256
            or result.byte_count != request.byte_count
        ):
            raise ImageConflict("image recipe input changed during processing")
        row.recipe = result.recipe.model_dump(mode="json")
        await settle_storage_hold(session, row.owner_user_id, row.held_bytes, row.original_bytes)
        row.held_bytes = 0
        row.state = "ready"
        row.updated_at = datetime.now(UTC)
    processed_total.add(1, {"outcome": "ready"})


async def drive_normalized_input(input_id: uuid.UUID) -> None:
    """Normalize once by a stable output UUID, then settle original and derived bytes."""
    settings = get_settings()
    async with session_scope() as session:
        row = await session.get(ImageInputRow, input_id)
        if row is None or row.state in {"ready", "failed", "deleting", "purged"}:
            return
        if (
            row.state != "processing"
            or row.purpose not in {"init", "mask", "control"}
            or row.deleted_at is not None
            or row.original_key != str(row.id)
            or row.original_sha256 is None
            or row.processing_job_id is None
            or row.held_bytes < row.original_bytes
            or row.original_bytes <= 0
        ):
            row.state = "failed"
            row.updated_at = datetime.now(UTC)
            processed_total.add(1, {"outcome": "refused"})
            return
        request = ImageFileProcessRequest(
            job_id=row.processing_job_id,
            input_id=row.id,
            source_sha256=row.original_sha256,
            purpose=cast("Literal['init', 'mask', 'control']", row.purpose),
            operation="normalize_mask" if row.purpose == "mask" else "normalize_image",
            byte_count=row.original_bytes,
            output_id=row.id,
            deadline_at=datetime.now(UTC)
            + timedelta(seconds=settings.file_worker_process_timeout_s),
        )
        owner_id = row.owner_user_id

    with tracer.start_as_current_span("coire.scheduler.image_input.normalize"):
        try:
            async with FileWorkerClient(settings) as client:
                result = await client.process_image_input(request)
        except FileWorkerParseRefused:
            async with session_scope() as session:
                row = await session.get(ImageInputRow, input_id, with_for_update=True)
                if (
                    row is not None
                    and row.state == "processing"
                    and row.processing_job_id == request.job_id
                ):
                    row.state = "failed"
                    row.updated_at = datetime.now(UTC)
            processed_total.add(1, {"outcome": "refused"})
            return

    if (
        result.output_id != input_id
        or result.normalized_bytes is None
        or result.normalized_sha256 is None
        or result.width is None
        or result.height is None
        or not await asyncio.to_thread(
            _verified_normalized,
            Path(settings.image_input_derived_root) / str(input_id),
            result,
        )
    ):
        raise ImageConflict("normalized image input is unavailable")
    async with session_scope() as session:
        # Settlement must not invert deletion/maintenance lock order.
        await session.execute(_QUOTA_LOCK)
        row = await session.get(ImageInputRow, input_id, with_for_update=True)
        if row is None or row.state != "processing":
            return
        if (
            row.owner_user_id != owner_id
            or row.processing_job_id != request.job_id
            or row.original_sha256 != request.source_sha256
            or row.original_bytes != request.byte_count
            or row.held_bytes < row.original_bytes + result.normalized_bytes
        ):
            raise ImageConflict("image input changed during normalization")
        row.normalized_key = str(input_id)
        row.normalized_bytes = result.normalized_bytes
        row.normalized_sha256 = result.normalized_sha256
        row.normalized_width = result.width
        row.normalized_height = result.height
        await settle_storage_hold(
            session, row.owner_user_id, row.held_bytes, row.original_bytes + result.normalized_bytes
        )
        row.held_bytes = 0
        row.state = "ready"
        row.updated_at = datetime.now(UTC)
    processed_total.add(1, {"outcome": "ready"})


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1.0)
async def image_recipe_step(input_id: str) -> None:
    await drive_recipe_input(uuid.UUID(input_id))


@DBOS.workflow(name="coire.image.input.recipe", max_recovery_attempts=100)
async def image_recipe_workflow(input_id: str) -> None:
    await image_recipe_step(input_id)


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1.0)
async def image_normalize_step(input_id: str) -> None:
    await drive_normalized_input(uuid.UUID(input_id))


@DBOS.workflow(name="coire.image.input.normalize", max_recovery_attempts=100)
async def image_normalize_workflow(input_id: str) -> None:
    await image_normalize_step(input_id)
