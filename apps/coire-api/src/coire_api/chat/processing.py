"""API-side verification and atomic publication of isolated file-worker output."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from opentelemetry import metrics, trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.chat.file_manifest import validate_result
from coire_api.chat.files import project_attachment
from coire_api.db import (
    ChatAttachmentRow,
    ChatConversationRow,
    ChatEventRow,
    ChatFileProcessingRow,
    ChatQuotaReservationRow,
    UserRow,
    session_scope,
)
from coire_core.models.chat import ChatAttachmentChanged, ChatEvent
from coire_core.models.files import FileProcessAsset, FileProcessRequest, FileProcessResult
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.chat.processing")
publication_total = metrics.get_meter("coire.api.chat.processing").create_counter(
    "coire_chat_file_publication_total", unit="1", description="Verified file publication outcomes"
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_ASSET_BYTES = 32 * 1024 * 1024


class InvalidAsset(ValueError):
    """Stored output is missing or differs from its immutable worker manifest."""


def verify_asset(root: Path, job_id: str, asset: FileProcessAsset) -> None:
    """Read through no-follow generated IDs and compare bounded bytes and PNG dimensions."""

    if asset.media_type != "image/png":
        raise InvalidAsset("invalid asset type")
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        directory_fd = os.open(root / job_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        file_fd = os.open(f"{asset.id}.png", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != asset.bytes:
            raise InvalidAsset("asset size mismatch")
        with os.fdopen(file_fd, "rb", closefd=False) as source:
            data = source.read(min(asset.bytes, MAX_ASSET_BYTES) + 1)
    except OSError as exc:
        raise InvalidAsset("asset unavailable") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)
    if len(data) != asset.bytes or hashlib.sha256(data).hexdigest() != asset.sha256:
        raise InvalidAsset("asset digest mismatch")
    if (
        len(data) < 24
        or data[:8] != PNG_SIGNATURE
        or data[8:16] != b"\x00\x00\x00\rIHDR"
        or int.from_bytes(data[16:20], "big") != asset.width
        or int.from_bytes(data[20:24], "big") != asset.height
    ):
        raise InvalidAsset("asset dimensions mismatch")


def _append_event(
    session: AsyncSession,
    conversation: ChatConversationRow,
    attachment: ChatAttachmentRow,
    settings: Settings,
) -> None:
    now = datetime.now(UTC)
    conversation.revision += 1
    conversation.event_cursor += 1
    conversation.updated_at = now
    event = ChatEvent(
        conversation_id=conversation.id,
        cursor=conversation.event_cursor,
        created_at=now,
        payload=ChatAttachmentChanged(attachment=project_attachment(attachment)),
    )
    session.add(
        ChatEventRow(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            cursor=event.cursor,
            type=event.payload.type,
            payload=event.payload.model_dump(mode="json"),
            created_at=now,
            expires_at=now + timedelta(hours=settings.chat_event_retention_hours),
        )
    )


async def publish_processed_job(job_id: str, settings: Settings) -> str:
    """Verify one immutable result and commit its scoped visibility exactly once."""

    with tracer.start_as_current_span("coire.api.chat.file_publish") as span:
        span.set_attribute("job_id", job_id)
        async with session_scope() as session:
            snapshot = await session.get(ChatFileProcessingRow, job_id)
            if snapshot is None or snapshot.state != "processed":
                return "unchanged"
            if snapshot.owner_user_id is None or snapshot.attachment_id is None:
                snapshot.state = "failed"
                snapshot.safe_error = "source_identity_mismatch"
                return "failed"
            await session.get(UserRow, snapshot.owner_user_id, with_for_update=True)
            attachment = await session.get(ChatAttachmentRow, snapshot.attachment_id)
            if attachment is None:
                job = await session.get(ChatFileProcessingRow, job_id, with_for_update=True)
                if job is not None and job.state == "processed":
                    job.state = "cancelled"
                return "cancelled"
            conversation = await session.get(
                ChatConversationRow, attachment.conversation_id, with_for_update=True
            )
            job = await session.get(ChatFileProcessingRow, job_id, with_for_update=True)
            if job is None or job.state != "processed":
                return "unchanged"
            attachment = await session.get(
                ChatAttachmentRow, job.attachment_id, with_for_update=True
            )
            if (
                attachment is None
                or conversation is None
                or attachment.deleted_at is not None
                or conversation.deleted_at is not None
            ):
                job.state = "cancelled"
                job.updated_at = datetime.now(UTC)
                return "cancelled"
            reservation = await session.scalar(
                select(ChatQuotaReservationRow)
                .where(
                    ChatQuotaReservationRow.attachment_id == attachment.id,
                    ChatQuotaReservationRow.job_id == job.id,
                    ChatQuotaReservationRow.state == "active",
                )
                .with_for_update()
            )
            try:
                if (
                    job.owner_user_id != attachment.owner_user_id
                    or job.owner_user_id != conversation.owner_user_id
                    or job.source_key != str(attachment.id)
                    or attachment.original_key != str(attachment.id)
                    or job.source_sha256 != attachment.original_sha256
                    or reservation is None
                ):
                    raise InvalidAsset("source identity mismatch")
                manifest = job.output_manifest or {}
                request = FileProcessRequest.model_validate(manifest["request"])
                result = FileProcessResult.model_validate(manifest["result"])
                if (
                    request.job_id != job.id
                    or request.input_id != attachment.id
                    or request.source_sha256 != attachment.original_sha256
                    or request.operation != job.operation
                    or request.selected_pages != (job.selected_pages or [])
                    or (request.operation == "inspect" and len(request.output_ids) != 1)
                ):
                    raise InvalidAsset("request identity mismatch")
                validate_result(request, result)
                derived_bytes = sum(asset.bytes for asset in result.assets)
                if derived_bytes > settings.chat_derived_job_max_bytes:
                    raise InvalidAsset("derived quota mismatch")
                for asset in result.assets:
                    await asyncio.to_thread(
                        verify_asset, Path(settings.chat_derived_root), job.id, asset
                    )
                assert reservation is not None
                if reservation.reserved_bytes < attachment.original_bytes + derived_bytes:
                    raise InvalidAsset("reservation mismatch")
            except (KeyError, TypeError, ValueError):
                job.state = "failed"
                job.safe_error = "invalid_worker_output"
                attachment.state = "failed"
                attachment.safe_error = job.safe_error
                now = datetime.now(UTC)
                job.updated_at = now
                attachment.updated_at = now
                _append_event(session, conversation, attachment, settings)
                outcome = "failed"
            else:
                now = datetime.now(UTC)
                assert reservation is not None
                reservation.reserved_bytes = attachment.original_bytes + derived_bytes
                attachment.derived_bytes = derived_bytes
                attachment.detected_type = result.detected_type or "application/octet-stream"
                attachment.page_count = result.page_count
                attachment.extraction_status = (
                    "complete" if result.extracted_text is not None else "none"
                )
                attachment.asset_manifest = {
                    "job_id": job.id,
                    "result": result.model_dump(mode="json"),
                }
                attachment.state = "ready"
                attachment.safe_error = None
                attachment.updated_at = now
                job.state = "ready"
                job.safe_error = None
                job.updated_at = now
                _append_event(session, conversation, attachment, settings)
                outcome = "ready"
        publication_total.add(1, {"outcome": outcome})
        logger.info("file publication finished job_id=%s outcome=%s", job_id, outcome)
        return outcome


async def publish_processed_files(settings: Settings) -> int:
    """Bounded maintenance scan; row locks keep concurrent API instances idempotent."""

    async with session_scope() as session:
        job_ids = list(
            (
                await session.execute(
                    select(ChatFileProcessingRow.id)
                    .where(ChatFileProcessingRow.state == "processed")
                    .order_by(ChatFileProcessingRow.updated_at, ChatFileProcessingRow.id)
                    .limit(20)
                )
            ).scalars()
        )
    completed = 0
    for job_id in job_ids:
        if await publish_processed_job(job_id, settings) in {"ready", "failed", "cancelled"}:
            completed += 1
    return completed
