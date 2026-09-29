"""Restart-safe DBOS dispatch for private CPU file processing."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from dbos import DBOS
from opentelemetry import metrics, trace
from sqlalchemy import func, select

from coire_api.chat.file_manifest import validate_result
from coire_api.db import (
    ChatAttachmentRow,
    ChatConversationRow,
    ChatFileProcessingRow,
    session_scope,
)
from coire_api.file_worker_client import (
    FileWorkerBusy,
    FileWorkerClient,
    FileWorkerError,
    FileWorkerMissing,
)
from coire_core.models.files import FileProcessRequest, FileProcessResult
from coire_core.settings import Settings, get_settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.scheduler.files")
processing_total = metrics.get_meter("coire.scheduler.files").create_counter(
    "coire_file_dispatch_total", unit="1", description="Durable private file dispatch outcomes"
)
TERMINAL = {"processed", "ready", "failed", "cancelled", "purging", "purged"}
POLL_SECONDS = 0.5
purge_total = metrics.get_meter("coire.scheduler.files").create_counter(
    "coire_file_output_purge_total", unit="1", description="Private derived output purge outcomes"
)
failed_cleanup_total = metrics.get_meter("coire.scheduler.files").create_counter(
    "coire_failed_file_output_cleanup_total",
    unit="1",
    description="Failed conversion output cleanup outcomes",
)


async def _tombstoned(job: ChatFileProcessingRow) -> bool:
    if job.attachment_id is None:
        return False
    async with session_scope() as session:
        attachment = await session.get(ChatAttachmentRow, job.attachment_id)
        if attachment is None or attachment.deleted_at is not None:
            return True
        conversation = await session.get(ChatConversationRow, attachment.conversation_id)
        return conversation is None or conversation.deleted_at is not None


async def _prepare(job_id: str, settings: Settings) -> tuple[FileProcessRequest, bool] | None:
    """Commit running/request before any external POST; recover from it by GET only."""

    async with session_scope() as session:
        job = await session.get(ChatFileProcessingRow, job_id, with_for_update=True)
        if job is None or job.state in TERMINAL:
            return None
        if job.attachment_id is None:
            job.state = "failed"
            job.safe_error = "temporary_jobs_not_supported"
            return None
        attachment = await session.get(ChatAttachmentRow, job.attachment_id)
        conversation = (
            await session.get(ChatConversationRow, attachment.conversation_id)
            if attachment is not None
            else None
        )
        if (
            attachment is None
            or conversation is None
            or attachment.deleted_at is not None
            or conversation.deleted_at is not None
        ):
            job.state = "cancelled"
            return None
        if (
            job.source_key != str(attachment.id)
            or attachment.original_key != str(attachment.id)
            or job.source_sha256 != attachment.original_sha256
            or job.owner_user_id != attachment.owner_user_id
            or attachment.owner_user_id != conversation.owner_user_id
        ):
            job.state = "failed"
            job.safe_error = "source_identity_mismatch"
            attachment.state = "failed"
            attachment.safe_error = job.safe_error
            return None
        if job.state == "running":
            try:
                manifest = job.output_manifest or {}
                request = FileProcessRequest.model_validate(manifest["request"])
                if (
                    request.job_id != job.id
                    or request.input_id != attachment.id
                    or request.source_sha256 != job.source_sha256
                    or request.operation != job.operation
                    or request.selected_pages != (job.selected_pages or [])
                ):
                    raise ValueError("persisted worker request mismatch")
                return request, False
            except (KeyError, ValueError, TypeError):
                job.state = "failed"
                job.safe_error = "missing_worker_request"
                attachment.state = "failed"
                attachment.safe_error = job.safe_error
                return None
        if job.state != "queued":
            return None
        deadline = datetime.now(UTC) + timedelta(seconds=settings.file_worker_process_timeout_s)
        output_ids = (
            [uuid.uuid4() for _ in job.selected_pages]
            if job.operation == "render"
            else [uuid.uuid4()]
        )
        try:
            request = FileProcessRequest(
                job_id=job.id,
                input_id=attachment.id,
                source_sha256=attachment.original_sha256,
                operation=cast(Literal["inspect", "render"], job.operation),
                selected_pages=job.selected_pages or [],
                output_ids=output_ids,
                deadline_at=deadline,
            )
        except ValueError:
            job.state = "failed"
            job.safe_error = "invalid_job_selection"
            attachment.state = "failed"
            attachment.safe_error = job.safe_error
            return None
        job.deadline_at = deadline
        job.output_manifest = {"request": request.model_dump(mode="json")}
        job.state = "running"
        job.updated_at = datetime.now(UTC)
        return request, True


async def _finish(
    job_id: str,
    state: Literal["processed", "failed", "cancelled"],
    request: FileProcessRequest,
    result: FileProcessResult | None = None,
    safe_error: str | None = None,
) -> str:
    async with session_scope() as session:
        job = await session.get(ChatFileProcessingRow, job_id, with_for_update=True)
        if job is None or job.state in TERMINAL:
            return "unchanged"
        attachment = (
            await session.get(ChatAttachmentRow, job.attachment_id)
            if job.attachment_id is not None
            else None
        )
        conversation = (
            await session.get(ChatConversationRow, attachment.conversation_id)
            if attachment is not None
            else None
        )
        if (
            attachment is None
            or conversation is None
            or attachment.deleted_at is not None
            or conversation.deleted_at is not None
        ):
            state = "cancelled"
            result = None
        job.state = state
        job.updated_at = datetime.now(UTC)
        if state == "processed" and result is not None:
            job.output_manifest = {
                "request": request.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
            }
            job.safe_error = None
        else:
            job.safe_error = safe_error if state == "failed" else None
            if attachment is not None and state == "failed":
                attachment.state = "failed"
                attachment.safe_error = safe_error
                attachment.updated_at = datetime.now(UTC)
        return state


async def drive_file_job(job_id: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    with tracer.start_as_current_span("coire.scheduler.files.process") as span:
        span.set_attribute("job_id", job_id)
        prepared = await _prepare(job_id, settings)
        if prepared is None:
            async with session_scope() as session:
                cancelled = await session.get(ChatFileProcessingRow, job_id)
            if cancelled is not None and cancelled.state == "cancelled":
                async with FileWorkerClient(settings) as client:
                    with suppress(FileWorkerError):
                        await client.cancel(job_id)
            return
        request, new = prepared
        outcome = "failed"
        async with FileWorkerClient(settings) as client:
            try:
                if new:
                    while True:
                        if datetime.now(UTC) >= request.deadline_at:
                            raise FileWorkerBusy("worker busy beyond deadline")
                        try:
                            status = await client.process(request)
                            break
                        except FileWorkerBusy:
                            await asyncio.sleep(POLL_SECONDS)
                        except FileWorkerError:
                            # POST may have reached the worker. Never send it again.
                            status = await client.status(job_id)
                            break
                    # A failed/ambiguous POST may have been accepted. Query status only.
                else:
                    status = await client.status(job_id)
                while status.state in {"queued", "running"}:
                    async with session_scope() as session:
                        job = await session.get(ChatFileProcessingRow, job_id)
                    if job is None or await _tombstoned(job):
                        with suppress(FileWorkerError):
                            await client.cancel(job_id)
                        outcome = await _finish(job_id, "cancelled", request)
                        return
                    if datetime.now(UTC) >= request.deadline_at + timedelta(seconds=2):
                        raise FileWorkerError("worker deadline exceeded")
                    await asyncio.sleep(POLL_SECONDS)
                    status = await client.status(job_id)
                if status.state in {"processed", "ready"}:
                    if status.result is None:
                        raise ValueError("worker result missing")
                    validate_result(request, status.result)
                    outcome = await _finish(job_id, "processed", request, status.result)
                elif status.state == "cancelled":
                    outcome = await _finish(job_id, "cancelled", request)
                else:
                    error = status.safe_error or "processing_failed"
                    safe = (
                        error
                        if error.replace("_", "").isalnum() and len(error) <= 64
                        else "processing_failed"
                    )
                    outcome = await _finish(job_id, "failed", request, safe_error=safe)
            except FileWorkerMissing:
                outcome = await _finish(
                    job_id, "failed", request, safe_error="worker_status_missing"
                )
            except FileWorkerBusy:
                outcome = await _finish(job_id, "failed", request, safe_error="worker_busy")
            except (FileWorkerError, ValueError) as exc:
                code = (
                    "invalid_worker_manifest"
                    if isinstance(exc, ValueError)
                    else "worker_unavailable"
                )
                outcome = await _finish(job_id, "failed", request, safe_error=code)
            finally:
                processing_total.add(1, {"outcome": outcome})
                logger.info("file dispatch finished job_id=%s outcome=%s", job_id, outcome)


async def purge_deleted_file_outputs(settings: Settings) -> int:
    """Repeat idempotent worker erasure until every deleted parent's job is marked purged."""

    cutoff = datetime.now(UTC) - timedelta(seconds=2)
    async with session_scope() as session:
        job_ids = list(
            (
                await session.execute(
                    select(ChatFileProcessingRow.id)
                    .join(
                        ChatAttachmentRow,
                        ChatAttachmentRow.id == ChatFileProcessingRow.attachment_id,
                    )
                    .join(
                        ChatConversationRow,
                        ChatConversationRow.id == ChatAttachmentRow.conversation_id,
                    )
                    .where(
                        ChatConversationRow.deleted_at.is_not(None),
                        ChatFileProcessingRow.state != "purged",
                        ChatFileProcessingRow.deadline_at <= cutoff,
                    )
                    .order_by(ChatFileProcessingRow.created_at, ChatFileProcessingRow.id)
                    .limit(10)
                )
            ).scalars()
        )
    purged = 0
    for job_id in job_ids:
        with tracer.start_as_current_span("coire.scheduler.files.purge") as span:
            span.set_attribute("job_id", job_id)
            async with session_scope() as session:
                job = await session.get(ChatFileProcessingRow, job_id, with_for_update=True)
                if job is None or job.state == "purged" or job.deadline_at > cutoff:
                    continue
                attachment = await session.get(ChatAttachmentRow, job.attachment_id)
                conversation = (
                    await session.get(ChatConversationRow, attachment.conversation_id)
                    if attachment is not None
                    else None
                )
                if conversation is None or conversation.deleted_at is None:
                    continue
                job.state = "purging"
                job.updated_at = datetime.now(UTC)
            try:
                async with FileWorkerClient(settings) as client:
                    await client.purge(job_id)
            except FileWorkerError:
                purge_total.add(1, {"outcome": "retry"})
                logger.info("file output purge deferred job_id=%s", job_id)
                continue
            async with session_scope() as session:
                job = await session.get(ChatFileProcessingRow, job_id, with_for_update=True)
                if job is not None and job.state == "purging":
                    job.state = "purged"
                    job.output_manifest = None
                    job.updated_at = datetime.now(UTC)
                    purged += 1
            purge_total.add(1, {"outcome": "purged"})
            logger.info("file output purged job_id=%s", job_id)
    return purged


async def purge_failed_file_outputs(settings: Settings) -> int:
    """Clear crash remnants before a failed job becomes eligible for explicit retry."""

    async with session_scope() as session:
        job_ids = list(
            (
                await session.execute(
                    select(ChatFileProcessingRow.id)
                    .where(
                        ChatFileProcessingRow.state == "failed",
                        func.coalesce(
                            ChatFileProcessingRow.output_manifest["output_purged"].as_boolean(),
                            False,
                        ).is_(False),
                    )
                    .order_by(ChatFileProcessingRow.updated_at, ChatFileProcessingRow.id)
                    .limit(3)
                )
            ).scalars()
        )
    cleared = 0
    for job_id in job_ids:
        with tracer.start_as_current_span("coire.scheduler.files.failed_cleanup") as span:
            span.set_attribute("job_id", job_id)
            try:
                async with FileWorkerClient(settings) as client:
                    await client.purge(job_id)
            except FileWorkerError:
                failed_cleanup_total.add(1, {"outcome": "retry"})
                logger.info("failed file output cleanup deferred job_id=%s", job_id)
                continue
            async with session_scope() as session:
                job = await session.get(ChatFileProcessingRow, job_id, with_for_update=True)
                if job is not None and job.state == "failed":
                    job.output_manifest = {"output_purged": True}
                    job.updated_at = datetime.now(UTC)
                    cleared += 1
            failed_cleanup_total.add(1, {"outcome": "purged"})
            logger.info("failed file output cleaned job_id=%s", job_id)
    return cleared


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1.0)
async def file_processing_step(job_id: str) -> None:
    await drive_file_job(job_id)


@DBOS.workflow(name="coire.file.process", max_recovery_attempts=100)
async def file_processing_workflow(job_id: str) -> None:
    await file_processing_step(job_id)
