"""Owner-scoped original storage and quota admission for native Chat."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    ChatAttachmentRow,
    ChatConversationRow,
    ChatEventRow,
    ChatFileProcessingRow,
    ChatQuotaReservationRow,
    UserRow,
)
from coire_core.errors import ChatConflict, ChatNotFound, ChatQuotaExceeded
from coire_core.models.chat import ChatAttachmentChanged, ChatEvent
from coire_core.models.files import (
    ChatAttachment,
    ChatFileProcessRequest,
    ChatPreviewAsset,
    ChatUploadMetadata,
    FileProcessResult,
)
from coire_core.settings import Settings

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CHUNK = 64 * 1024


def _write_all(fd: int, chunk: bytes) -> None:
    view = memoryview(chunk)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("original write failed")
        view = view[count:]


def new_job_id() -> str:
    value = (int(datetime.now(UTC).timestamp() * 1000) << 80) | secrets.randbits(80)
    return "".join(ALPHABET[(value >> (5 * shift)) & 31] for shift in range(25, -1, -1))


@dataclass(frozen=True, slots=True)
class StagedOriginal:
    id: uuid.UUID
    temporary: Path
    target: Path
    size: int
    sha256: str

    def publish(self) -> None:
        os.link(self.temporary, self.target, follow_symlinks=False)
        try:
            self.temporary.unlink()
        except OSError:
            self.target.unlink(missing_ok=True)
            raise

    def discard(self) -> None:
        self.temporary.unlink(missing_ok=True)


async def stage_original(file: UploadFile, root: Path, max_bytes: int) -> StagedOriginal:
    """Read a bounded upload to a generated temporary key without trusting filename/MIME."""

    original_id = uuid.uuid4()
    temporary = root / f".{original_id}.{uuid.uuid4()}.uploading"
    target = root / str(original_id)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ChatConflict("file storage unavailable") from exc
    size = 0
    digest = hashlib.sha256()
    try:
        try:
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ChatQuotaExceeded("original exceeds 10 MiB")
                digest.update(chunk)
                await asyncio.to_thread(_write_all, fd, chunk)
            if size == 0:
                raise ChatConflict("empty files are not supported")
            await asyncio.to_thread(os.fsync, fd)
        finally:
            os.close(fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return StagedOriginal(original_id, temporary, target, size, digest.hexdigest())


def project_attachment(row: ChatAttachmentRow) -> ChatAttachment:
    previews: list[ChatPreviewAsset] = []
    if row.state == "ready" and row.asset_manifest is not None:
        try:
            result = FileProcessResult.model_validate(row.asset_manifest["result"])
            if (
                row.asset_manifest["job_id"] != result.job_id
                or result.input_id != row.id
                or any(asset.media_type != "image/png" for asset in result.assets)
            ):
                raise ValueError("preview identity mismatch")
            previews = [
                ChatPreviewAsset(
                    id=asset.id,
                    media_type="image/png",
                    width=asset.width,
                    height=asset.height,
                    page=asset.page,
                )
                for asset in result.assets
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise ChatConflict("file preview unavailable") from exc
    return ChatAttachment(
        id=row.id,
        owner_id=row.owner_user_id,
        conversation_id=row.conversation_id,
        filename=row.filename,
        detected_type=row.detected_type,
        original_bytes=row.original_bytes,
        original_sha256=row.original_sha256,
        derived_bytes=row.derived_bytes,
        state=cast(Literal["uploading", "processing", "ready", "failed", "deleting"], row.state),
        page_count=row.page_count,
        previews=previews,
        safe_error=row.safe_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def owned_attachment(
    session: AsyncSession, principal: Principal, conversation_id: uuid.UUID, file_id: uuid.UUID
) -> ChatAttachmentRow:
    attachment = await session.get(ChatAttachmentRow, file_id)
    if (
        attachment is None
        or attachment.conversation_id != conversation_id
        or attachment.owner_user_id != principal.user_id
        or attachment.deleted_at is not None
    ):
        raise ChatNotFound()
    conversation = await session.get(ChatConversationRow, conversation_id)
    if (
        conversation is None
        or conversation.owner_user_id != principal.user_id
        or conversation.deleted_at is not None
    ):
        raise ChatNotFound()
    return attachment


async def admit_original(
    session: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    metadata: ChatUploadMetadata,
    staged: StagedOriginal,
    settings: Settings,
) -> ChatAttachment:
    """Lock owner quota and conversation, then publish bytes and metadata together."""

    assert principal.user_id is not None
    published = False
    try:
        owner = await session.scalar(
            select(UserRow).where(UserRow.id == principal.user_id).with_for_update()
        )
        if owner is None:
            raise ChatNotFound()
        conversation = await session.scalar(
            select(ChatConversationRow)
            .where(ChatConversationRow.id == conversation_id)
            .with_for_update()
        )
        if (
            conversation is None
            or conversation.owner_user_id != principal.user_id
            or conversation.deleted_at is not None
        ):
            raise ChatNotFound()
        if conversation.revision != metadata.expected_revision:
            raise ChatConflict("conversation changed; refresh before uploading")
        reserved = staged.size + settings.chat_derived_job_max_bytes
        owner_used = await session.scalar(
            select(func.coalesce(func.sum(ChatQuotaReservationRow.reserved_bytes), 0)).where(
                ChatQuotaReservationRow.owner_user_id == principal.user_id,
                ChatQuotaReservationRow.state == "active",
            )
        )
        conversation_used = await session.scalar(
            select(func.coalesce(func.sum(ChatQuotaReservationRow.reserved_bytes), 0)).where(
                ChatQuotaReservationRow.conversation_id == conversation_id,
                ChatQuotaReservationRow.state == "active",
            )
        )
        if (
            int(owner_used or 0) + reserved > settings.chat_owner_quota_bytes
            or int(conversation_used or 0) + reserved > settings.chat_conversation_quota_bytes
        ):
            raise ChatQuotaExceeded("chat file quota exceeded")
        now = datetime.now(UTC)
        attachment = ChatAttachmentRow(
            id=staged.id,
            owner_user_id=principal.user_id,
            conversation_id=conversation_id,
            filename=metadata.filename,
            detected_type="application/octet-stream",
            original_bytes=staged.size,
            original_sha256=staged.sha256,
            original_key=str(staged.id),
            derived_bytes=0,
            state="processing",
            created_at=now,
            updated_at=now,
        )
        session.add(attachment)
        await session.flush()
        job = ChatFileProcessingRow(
            id=new_job_id(),
            attachment_id=attachment.id,
            owner_user_id=principal.user_id,
            principal_kind="user",
            principal_subject=str(principal.user_id),
            request_id=uuid.uuid4(),
            operation="inspect",
            source_key=str(staged.id),
            source_sha256=staged.sha256,
            selected_pages=[],
            state="queued",
            attempt=1,
            deadline_at=now + timedelta(seconds=settings.file_worker_process_timeout_s),
            expires_at=now + timedelta(hours=settings.chat_purge_deadline_hours),
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        await session.flush()
        session.add(
            ChatQuotaReservationRow(
                id=uuid.uuid4(),
                owner_user_id=principal.user_id,
                conversation_id=conversation_id,
                attachment_id=attachment.id,
                job_id=job.id,
                reserved_bytes=reserved,
                state="active",
                expires_at=job.expires_at,
                created_at=now,
            )
        )
        result = project_attachment(attachment)
        conversation.revision += 1
        conversation.event_cursor += 1
        conversation.updated_at = now
        event = ChatEvent(
            conversation_id=conversation_id,
            cursor=conversation.event_cursor,
            created_at=now,
            payload=ChatAttachmentChanged(attachment=result),
        )
        session.add(
            ChatEventRow(
                id=uuid.uuid4(),
                conversation_id=conversation_id,
                cursor=event.cursor,
                type=event.payload.type,
                payload=event.payload.model_dump(mode="json"),
                created_at=now,
                expires_at=now + timedelta(hours=settings.chat_event_retention_hours),
            )
        )
        await asyncio.to_thread(staged.publish)
        published = True
        await session.commit()
        return result
    except BaseException:
        try:
            if published:
                staged.target.unlink(missing_ok=True)
            staged.discard()
        finally:
            await asyncio.shield(session.rollback())
        raise


async def retry_inspection(
    session: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    file_id: uuid.UUID,
    body: ChatFileProcessRequest,
    settings: Settings,
) -> ChatAttachment:
    """Queue at most one explicit second inspect after verified failed-output cleanup."""

    assert principal.user_id is not None
    if body.operation != "inspect":
        raise ChatConflict("page rendering is not available yet")
    owner = await session.scalar(
        select(UserRow).where(UserRow.id == principal.user_id).with_for_update()
    )
    if owner is None:
        raise ChatNotFound()
    conversation = await session.scalar(
        select(ChatConversationRow)
        .where(ChatConversationRow.id == conversation_id)
        .with_for_update()
    )
    if (
        conversation is None
        or conversation.owner_user_id != principal.user_id
        or conversation.deleted_at is not None
    ):
        raise ChatNotFound()
    attachment = await session.scalar(
        select(ChatAttachmentRow).where(ChatAttachmentRow.id == file_id).with_for_update()
    )
    if (
        attachment is None
        or attachment.owner_user_id != principal.user_id
        or attachment.conversation_id != conversation_id
        or attachment.deleted_at is not None
    ):
        raise ChatNotFound()
    existing = await session.scalar(
        select(ChatFileProcessingRow).where(ChatFileProcessingRow.request_id == body.request_id)
    )
    if existing is not None:
        client_request = (existing.output_manifest or {}).get("client_request")
        if (
            existing.attachment_id != attachment.id
            or existing.owner_user_id != principal.user_id
            or existing.operation != "inspect"
            or existing.selected_pages
            or not isinstance(client_request, dict)
            or client_request.get("expected_revision") != body.expected_revision
        ):
            raise ChatConflict("request ID already used for a different file action")
        return project_attachment(attachment)
    if conversation.revision != body.expected_revision:
        raise ChatConflict("conversation changed; refresh before retrying")
    latest = await session.scalar(
        select(ChatFileProcessingRow)
        .where(
            ChatFileProcessingRow.attachment_id == attachment.id,
            ChatFileProcessingRow.operation == "inspect",
        )
        .order_by(ChatFileProcessingRow.attempt.desc(), ChatFileProcessingRow.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if (
        latest is None
        or latest.state != "failed"
        or latest.attempt != 1
        or attachment.state != "failed"
    ):
        raise ChatConflict("file is not eligible for another attempt")
    if not latest.output_manifest or latest.output_manifest.get("output_purged") is not True:
        raise ChatConflict("file cleanup is still pending")
    if (
        attachment.original_key != str(attachment.id)
        or latest.source_key != str(attachment.id)
        or latest.source_sha256 != attachment.original_sha256
    ):
        raise ChatConflict("file source changed")
    reservation = await session.scalar(
        select(ChatQuotaReservationRow)
        .where(
            ChatQuotaReservationRow.attachment_id == attachment.id,
            ChatQuotaReservationRow.job_id == latest.id,
            ChatQuotaReservationRow.state == "active",
        )
        .with_for_update()
    )
    if (
        reservation is None
        or reservation.reserved_bytes
        < attachment.original_bytes + settings.chat_derived_job_max_bytes
    ):
        raise ChatConflict("file reservation unavailable")
    now = datetime.now(UTC)
    next_job = ChatFileProcessingRow(
        id=new_job_id(),
        attachment_id=attachment.id,
        owner_user_id=principal.user_id,
        principal_kind="user",
        principal_subject=str(principal.user_id),
        request_id=body.request_id,
        operation="inspect",
        source_key=str(attachment.id),
        source_sha256=attachment.original_sha256,
        selected_pages=[],
        output_manifest={"client_request": {"expected_revision": body.expected_revision}},
        state="queued",
        attempt=2,
        deadline_at=now + timedelta(seconds=settings.file_worker_process_timeout_s),
        expires_at=now + timedelta(hours=settings.chat_purge_deadline_hours),
        created_at=now,
        updated_at=now,
    )
    session.add(next_job)
    await session.flush()
    reservation.job_id = next_job.id
    attachment.state = "processing"
    attachment.safe_error = None
    attachment.updated_at = now
    conversation.revision += 1
    conversation.event_cursor += 1
    conversation.updated_at = now
    response = project_attachment(attachment)
    event = ChatEvent(
        conversation_id=conversation_id,
        cursor=conversation.event_cursor,
        created_at=now,
        payload=ChatAttachmentChanged(attachment=response),
    )
    session.add(
        ChatEventRow(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            cursor=event.cursor,
            type=event.payload.type,
            payload=event.payload.model_dump(mode="json"),
            created_at=now,
            expires_at=now + timedelta(hours=settings.chat_event_retention_hours),
        )
    )
    await session.commit()
    return response


async def render_pdf_pages(
    session: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    file_id: uuid.UUID,
    body: ChatFileProcessRequest,
    settings: Settings,
) -> ChatAttachment:
    """Queue only the owner's explicit page selection under the file quota lock."""

    assert principal.user_id is not None
    if body.operation != "render":
        raise ChatConflict("render requires selected PDF pages")
    owner = await session.scalar(
        select(UserRow).where(UserRow.id == principal.user_id).with_for_update()
    )
    if owner is None:
        raise ChatNotFound()
    conversation = await session.scalar(
        select(ChatConversationRow)
        .where(ChatConversationRow.id == conversation_id)
        .with_for_update()
    )
    if (
        conversation is None
        or conversation.owner_user_id != principal.user_id
        or conversation.deleted_at is not None
    ):
        raise ChatNotFound()
    attachment = await session.scalar(
        select(ChatAttachmentRow).where(ChatAttachmentRow.id == file_id).with_for_update()
    )
    if (
        attachment is None
        or attachment.owner_user_id != principal.user_id
        or attachment.conversation_id != conversation_id
        or attachment.deleted_at is not None
    ):
        raise ChatNotFound()
    existing = await session.scalar(
        select(ChatFileProcessingRow).where(ChatFileProcessingRow.request_id == body.request_id)
    )
    if existing is not None:
        binding = (existing.output_manifest or {}).get("client_request")
        if (
            existing.attachment_id != attachment.id
            or existing.owner_user_id != principal.user_id
            or existing.operation != "render"
            or existing.selected_pages != body.selected_pages
            or not isinstance(binding, dict)
            or binding.get("expected_revision") != body.expected_revision
            or binding.get("selected_pages") != body.selected_pages
        ):
            raise ChatConflict("request ID already used for a different file action")
        return project_attachment(attachment)
    if conversation.revision != body.expected_revision:
        raise ChatConflict("conversation changed; refresh before selecting pages")
    if (
        attachment.detected_type != "application/pdf"
        or attachment.page_count is None
        or any(page > attachment.page_count for page in body.selected_pages)
    ):
        raise ChatConflict("selected pages exceed this PDF")
    latest = await session.scalar(
        select(ChatFileProcessingRow)
        .where(
            ChatFileProcessingRow.attachment_id == attachment.id,
            ChatFileProcessingRow.operation == "render",
        )
        .order_by(ChatFileProcessingRow.attempt.desc(), ChatFileProcessingRow.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if latest is None:
        if attachment.state != "ready" or attachment.asset_manifest is None:
            raise ChatConflict("PDF is not ready for page rendering")
        try:
            inspection = FileProcessResult.model_validate(attachment.asset_manifest["result"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ChatConflict("PDF extraction metadata unavailable") from exc
        if (
            inspection.detected_type != "application/pdf"
            or inspection.input_id != attachment.id
            or inspection.source_sha256 != attachment.original_sha256
            or inspection.extracted_text is None
            or inspection.assets
        ):
            raise ChatConflict("PDF extraction metadata unavailable")
        attempt = 1
    else:
        if (
            latest.state != "failed"
            or latest.attempt != 1
            or latest.selected_pages != body.selected_pages
            or attachment.state != "failed"
            or not latest.output_manifest
            or latest.output_manifest.get("output_purged") is not True
        ):
            raise ChatConflict("PDF page rendering is not eligible for another attempt")
        attempt = 2
    if attachment.original_key != str(attachment.id) or (
        latest is not None
        and (
            latest.source_key != str(attachment.id)
            or latest.source_sha256 != attachment.original_sha256
        )
    ):
        raise ChatConflict("file source changed")
    reservation = await session.scalar(
        select(ChatQuotaReservationRow)
        .where(
            ChatQuotaReservationRow.attachment_id == attachment.id,
            ChatQuotaReservationRow.state == "active",
        )
        .with_for_update()
    )
    if reservation is None:
        raise ChatConflict("file reservation unavailable")
    desired = attachment.original_bytes + settings.chat_derived_job_max_bytes
    if latest is None:
        if reservation.reserved_bytes < attachment.original_bytes:
            raise ChatConflict("file reservation unavailable")
        delta = max(0, desired - reservation.reserved_bytes)
        owner_used = await session.scalar(
            select(func.coalesce(func.sum(ChatQuotaReservationRow.reserved_bytes), 0)).where(
                ChatQuotaReservationRow.owner_user_id == principal.user_id,
                ChatQuotaReservationRow.state == "active",
            )
        )
        conversation_used = await session.scalar(
            select(func.coalesce(func.sum(ChatQuotaReservationRow.reserved_bytes), 0)).where(
                ChatQuotaReservationRow.conversation_id == conversation_id,
                ChatQuotaReservationRow.state == "active",
            )
        )
        if (
            int(owner_used or 0) + delta > settings.chat_owner_quota_bytes
            or int(conversation_used or 0) + delta > settings.chat_conversation_quota_bytes
        ):
            raise ChatQuotaExceeded("PDF page rendering exceeds chat file quota")
        reservation.reserved_bytes = max(reservation.reserved_bytes, desired)
    elif reservation.job_id != latest.id or reservation.reserved_bytes < desired:
        raise ChatConflict("file reservation unavailable")
    now = datetime.now(UTC)
    job = ChatFileProcessingRow(
        id=new_job_id(),
        attachment_id=attachment.id,
        owner_user_id=principal.user_id,
        principal_kind="user",
        principal_subject=str(principal.user_id),
        request_id=body.request_id,
        operation="render",
        source_key=str(attachment.id),
        source_sha256=attachment.original_sha256,
        selected_pages=body.selected_pages,
        output_manifest={
            "client_request": {
                "expected_revision": body.expected_revision,
                "selected_pages": body.selected_pages,
            }
        },
        state="queued",
        attempt=attempt,
        deadline_at=now + timedelta(seconds=settings.file_worker_process_timeout_s),
        expires_at=now + timedelta(hours=settings.chat_purge_deadline_hours),
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    await session.flush()
    reservation.job_id = job.id
    attachment.state = "processing"
    attachment.safe_error = None
    attachment.updated_at = now
    conversation.revision += 1
    conversation.event_cursor += 1
    conversation.updated_at = now
    response = project_attachment(attachment)
    event = ChatEvent(
        conversation_id=conversation_id,
        cursor=conversation.event_cursor,
        created_at=now,
        payload=ChatAttachmentChanged(attachment=response),
    )
    session.add(
        ChatEventRow(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            cursor=event.cursor,
            type=event.payload.type,
            payload=event.payload.model_dump(mode="json"),
            created_at=now,
            expires_at=now + timedelta(hours=settings.chat_event_retention_hours),
        )
    )
    await session.commit()
    return response


def read_original(attachment: ChatAttachmentRow, root: Path) -> bytes:
    """Read only its generated key and verify the immutable upload manifest."""

    if attachment.original_key != str(attachment.id):
        raise ChatNotFound()
    try:
        fd = os.open(root / attachment.original_key, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ChatNotFound()
            with os.fdopen(fd, "rb", closefd=False) as source:
                data = source.read(attachment.original_bytes + 1)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ChatNotFound() from exc
    if (
        len(data) != attachment.original_bytes
        or hashlib.sha256(data).hexdigest() != attachment.original_sha256
    ):
        raise ChatNotFound()
    return data
