"""Restart-safe DBOS handoff for metadata-only owner recipe inputs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from dbos import DBOS
from opentelemetry import metrics, trace

from coire_api.db import ImageInputRow, session_scope
from coire_api.file_worker_client import FileWorkerClient, FileWorkerParseRefused
from coire_api.images.quota import settle_storage_hold
from coire_core.errors import ImageConflict
from coire_core.models.image_worker import ImageRecipeParseRequest
from coire_core.settings import get_settings

tracer = trace.get_tracer("coire.scheduler.image_inputs")
processed_total = metrics.get_meter("coire.scheduler.image_inputs").create_counter(
    "coire_image_input_processing_total", unit="1", description="Image input processing outcomes"
)


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


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1.0)
async def image_recipe_step(input_id: str) -> None:
    await drive_recipe_input(uuid.UUID(input_id))


@DBOS.workflow(name="coire.image.input.recipe", max_recovery_attempts=100)
async def image_recipe_workflow(input_id: str) -> None:
    await image_recipe_step(input_id)
