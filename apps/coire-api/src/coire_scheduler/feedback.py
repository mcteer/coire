"""Bounded privacy cleanup runs independently of preference admissions."""

import asyncio
import logging
from contextlib import suppress

from coire_api.db import session_scope
from coire_api.feedback.accounting import settle_comparisons
from coire_api.feedback.baseline import record_feedback_metrics
from coire_api.feedback.retention import expire_sources, purge_sources
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


async def cleanup_feedback(settings: Settings) -> int:
    async with session_scope() as session:
        await expire_sources(session, batch_size=settings.feedback_purge_batch_size)
        count = await purge_sources(session, batch_size=settings.feedback_purge_batch_size)
        await session.commit()
    async with session_scope() as session:
        await record_feedback_metrics(session)
    return count + await settle_comparisons(batch_size=settings.feedback_purge_batch_size)


class FeedbackCleanupWorker:
    """Withdrawal cleanup has its own loop and remains active with admissions disabled."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.task: asyncio.Task[None] | None = None
        self.stop_event = asyncio.Event()

    async def start(self) -> None:
        if self.task is None:
            self.stop_event.clear()
            self.task = asyncio.create_task(self.run(), name="feedback-cleanup")

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                count = await cleanup_feedback(self.settings)
                if count >= self.settings.feedback_purge_batch_size:
                    await asyncio.sleep(0)
                    continue
            except Exception as error:
                logger.error(
                    "feedback cleanup failed",
                    extra={"operation": "cleanup", "safe_reason": type(error).__name__},
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), timeout=5)
