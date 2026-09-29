"""Resolve immutable, owner-scoped extracted file text for a native Chat turn."""

from __future__ import annotations

import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ChatAttachmentRow
from coire_core.errors import ChatConflict, ChatContextExceeded, ChatNotFound
from coire_core.models.files import ChatAttachmentSelection, FileProcessResult

MAX_PROMPT_CHARACTERS = 2_000_000


def _published_text(attachment: ChatAttachmentRow) -> str:
    manifest = attachment.asset_manifest
    if not isinstance(manifest, dict):
        raise ChatConflict("file text is unavailable")
    try:
        current = FileProcessResult.model_validate(manifest["result"])
        result = FileProcessResult.model_validate(manifest.get("text_result") or manifest["result"])
        if (
            manifest["job_id"] != current.job_id
            or current.input_id != attachment.id
            or current.source_sha256 != attachment.original_sha256
            or current.detected_type != attachment.detected_type
            or result.input_id != attachment.id
            or result.source_sha256 != attachment.original_sha256
            or result.detected_type != attachment.detected_type
            or result.extracted_text is None
        ):
            raise ValueError("text identity mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        raise ChatConflict("file text is unavailable") from exc
    text = result.extracted_text
    if attachment.detected_type == "application/pdf":
        # PDFium can legitimately extract no Unicode from a scan. A successful parser
        # result must not become an empty prompt that silently omits the document.
        if not re.sub(r"\[Page [0-9]+\]", "", text).strip():
            raise ChatConflict("PDF has no extracted text; select page images")
    elif not text.strip():
        raise ChatConflict("file has no extracted text")
    return text


async def compose_text_prompt(
    session: AsyncSession,
    owner_id: uuid.UUID,
    conversation_id: uuid.UUID,
    content: str,
    selections: list[ChatAttachmentSelection],
) -> str:
    chunks = [content]
    for index, selection in enumerate(selections, 1):
        if selection.mode != "text":
            raise ChatConflict("image-capable Chat is not available yet")
        attachment = await session.get(ChatAttachmentRow, selection.file_id)
        if (
            attachment is None
            or attachment.owner_user_id != owner_id
            or attachment.conversation_id != conversation_id
            or attachment.deleted_at is not None
        ):
            raise ChatNotFound()
        if attachment.state != "ready":
            raise ChatConflict("file is not ready; wait for processing")
        if attachment.detected_type.startswith("image/"):
            raise ChatConflict("image requires an image-capable model")
        text = _published_text(attachment)
        chunks.append(
            f"\n\n[Attachment {index}: {attachment.filename}]\n{text}\n[/Attachment {index}]"
        )
        if sum(len(chunk) for chunk in chunks) > MAX_PROMPT_CHARACTERS:
            raise ChatContextExceeded("attachments exceed the maximum request size")
    return "".join(chunks)
