"""Rebuild owner-scoped visual Chat parts from verified private worker assets."""

from __future__ import annotations

import asyncio
import base64
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.chat.processing import InvalidAsset, read_private_preview
from coire_api.db import ChatAttachmentRow
from coire_core.errors import ChatConflict, ChatNotFound
from coire_core.models.files import ChatAttachmentSelection, FileProcessAsset, FileProcessResult
from coire_core.models.gateway import ChatMessage, OpenAIImagePart, OpenAIImageURL, OpenAITextPart
from coire_core.models.registry import VisualCapability


def _selected_assets(
    attachment: ChatAttachmentRow, selection: ChatAttachmentSelection
) -> list[FileProcessAsset]:
    manifest = attachment.asset_manifest
    if not isinstance(manifest, dict):
        raise ChatConflict("file preview unavailable")
    try:
        result = FileProcessResult.model_validate(manifest["result"])
        if (
            manifest["job_id"] != result.job_id
            or result.input_id != attachment.id
            or result.source_sha256 != attachment.original_sha256
            or result.detected_type != attachment.detected_type
        ):
            raise ValueError("preview identity mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        raise ChatConflict("file preview unavailable") from exc
    if attachment.detected_type.startswith("image/"):
        if selection.pages or len(result.assets) != 1 or result.assets[0].page is not None:
            raise ChatConflict("image selection changed; select it again")
        return result.assets
    if attachment.detected_type == "application/pdf":
        if not selection.pages or any(
            page > (attachment.page_count or 0) for page in selection.pages
        ):
            raise ChatConflict("select processed PDF pages")
        by_page = {asset.page: asset for asset in result.assets}
        if len(by_page) != len(result.assets) or any(
            page not in by_page for page in selection.pages
        ):
            raise ChatConflict("selected PDF pages are not ready")
        return [by_page[page] for page in selection.pages]
    raise ChatConflict("this file has no visual preview")


async def visual_message(
    session: AsyncSession,
    owner_id: uuid.UUID,
    conversation_id: uuid.UUID,
    text: str,
    selections: list[ChatAttachmentSelection],
    visual: VisualCapability | None,
    derived_root: Path,
) -> ChatMessage:
    """Use only the owner's exact published PNG IDs and digest-checked bytes."""

    chosen = [selection for selection in selections if selection.mode == "visual"]
    if not chosen:
        return ChatMessage(role="user", content=text)
    if visual is None or not visual.verified:
        raise ChatConflict("selected model cannot accept visual files")
    parts: list[OpenAITextPart | OpenAIImagePart] = [OpenAITextPart(text=text)]
    count = 0
    for index, selection in enumerate(chosen, 1):
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
        for asset in _selected_assets(attachment, selection):
            count += 1
            if count > visual.max_images:
                raise ChatConflict("too many images for the selected model")
            try:
                data = await asyncio.to_thread(
                    read_private_preview, attachment, asset.id, derived_root
                )
            except InvalidAsset as exc:
                raise ChatConflict("file preview unavailable") from exc
            page = f", page {asset.page}" if asset.page is not None else ""
            parts.append(OpenAITextPart(text=f"Attachment {index}: {attachment.filename}{page}"))
            parts.append(
                OpenAIImagePart(
                    image_url=OpenAIImageURL(
                        url="data:image/png;base64," + base64.b64encode(data).decode("ascii")
                    )
                )
            )
    return ChatMessage(role="user", content=parts)
