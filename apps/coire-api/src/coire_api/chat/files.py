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
from coire_core.models.files import ChatAttachment, ChatUploadMetadata
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
