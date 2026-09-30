"""Bounded API-side image blob purge; only the API mounts final private blobs."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace
from sqlalchemy import func, select

from coire_api.db import ImageOutputRow, ImageQuotaRow, session_scope
from coire_api.images.deletion import purge_output_blob
from coire_api.images.input_cleanup import (
    sweep_deleted_inputs,
    sweep_failed_inputs,
    sweep_orphan_inputs,
)
from coire_api.images.telemetry import purge_oldest_seconds, purges_total
from coire_core.errors import ImageStorageUnavailable
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.image")
_SWEEP_SECONDS = 30
_BATCH_SIZE = 25


async def purge_deleted_output(settings: Settings, output_id: uuid.UUID) -> bool:
    """Lock row and counters; a retry survives unlink-before-DB-commit crashes."""
    async with session_scope() as session:
        snapshot = await session.get(ImageOutputRow, output_id)
        if snapshot is None or snapshot.deleted_at is None or snapshot.purged_at is not None:
            return False
        global_quota = await session.scalar(
            select(ImageQuotaRow).where(ImageQuotaRow.scope == "global").with_for_update()
        )
        owner_quota = await session.scalar(
            select(ImageQuotaRow)
            .where(
                ImageQuotaRow.scope == "owner",
                ImageQuotaRow.owner_user_id == snapshot.owner_user_id,
            )
            .with_for_update()
        )
        if global_quota is None or owner_quota is None:
            raise ImageStorageUnavailable()
        row = await session.get(
            ImageOutputRow, output_id, populate_existing=True, with_for_update=True
        )
        if row is None or row.deleted_at is None or row.purged_at is not None:
            return False
        with tracer.start_as_current_span("coire.api.image.purge"):
            await asyncio.to_thread(
                purge_output_blob, Path(settings.image_blob_root), row, owner_quota, global_quota
            )
        return True


async def sweep_deleted_outputs(settings: Settings) -> int:
    async with session_scope() as session:
        pending = (
            await session.scalars(
                select(ImageOutputRow.id)
                .where(ImageOutputRow.deleted_at.is_not(None), ImageOutputRow.purged_at.is_(None))
                .order_by(ImageOutputRow.deleted_at, ImageOutputRow.id)
                .limit(_BATCH_SIZE)
            )
        ).all()
        oldest = await session.scalar(
            select(func.min(ImageOutputRow.deleted_at)).where(
                ImageOutputRow.deleted_at.is_not(None), ImageOutputRow.purged_at.is_(None)
            )
        )
    age = max(0.0, (datetime.now(UTC) - oldest).total_seconds()) if oldest else 0.0
    purge_oldest_seconds.set(age)
    purged = 0
    for output_id in pending:
        try:
            if await purge_deleted_output(settings, output_id):
                purged += 1
                purges_total.add(1, {"outcome": "succeeded"})
        except Exception as exc:
            purges_total.add(1, {"outcome": "failed"})
            logger.error(
                "image output purge failed output_id=%s error_type=%s",
                output_id,
                type(exc).__name__,
            )
    return purged


class ImageOutputMaintenance:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="image-output-maintenance")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await sweep_deleted_outputs(self._settings)
                await sweep_failed_inputs(self._settings)
                await sweep_deleted_inputs(self._settings)
                await sweep_orphan_inputs(self._settings)
            except Exception as exc:
                logger.error("image maintenance failed error_type=%s", type(exc).__name__)
            await asyncio.sleep(_SWEEP_SECONDS)
