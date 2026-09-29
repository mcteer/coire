"""Native visual history uses only owned, digest-checked worker outputs."""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.chat.visual_context import visual_message
from coire_api.db import ChatAttachmentRow
from coire_api.gateway.context import enforce_context
from coire_core.errors import ChatConflict, ChatNotFound
from coire_core.models.files import ChatAttachmentSelection, FileProcessAsset, FileProcessResult
from coire_core.models.registry import VisualCapability


async def test_visual_message_checks_owner_digest_and_measured_context(tmp_path: Path) -> None:
    owner, conversation, file_id, asset_id = (uuid.uuid4() for _ in range(4))
    job_id = "01K00000000000000000000000"
    output = io.BytesIO()
    Image.new("RGB", (16, 16), (255, 0, 0)).save(output, format="PNG", optimize=True)
    data = output.getvalue()
    folder = tmp_path / job_id
    folder.mkdir()
    (folder / f"{asset_id}.png").write_bytes(data)
    result = FileProcessResult(
        job_id=job_id,
        input_id=file_id,
        source_sha256="a" * 64,
        detected_type="image/png",
        assets=[
            FileProcessAsset(
                id=asset_id,
                sha256=hashlib.sha256(data).hexdigest(),
                bytes=len(data),
                media_type="image/png",
                width=16,
                height=16,
            )
        ],
    )
    now = datetime.now(UTC)
    attachment = ChatAttachmentRow(
        id=file_id,
        owner_user_id=owner,
        conversation_id=conversation,
        filename="red.png",
        detected_type="image/png",
        original_bytes=len(data),
        original_sha256="a" * 64,
        original_key=str(file_id),
        derived_bytes=len(data),
        state="ready",
        asset_manifest={"job_id": job_id, "result": result.model_dump(mode="json")},
        created_at=now,
        updated_at=now,
    )

    class Session:
        async def get(self, model: object, identifier: uuid.UUID) -> ChatAttachmentRow | None:
            return attachment if model is ChatAttachmentRow and identifier == file_id else None

    session = cast(AsyncSession, Session())
    selection = ChatAttachmentSelection(file_id=file_id, mode="visual")
    visual = VisualCapability(
        verified=True, max_images=1, max_image_pixels=256, max_encoded_bytes=90
    )
    message = await visual_message(
        session, owner, conversation, "What color?", [selection], visual, tmp_path
    )
    assert isinstance(message.content, list) and len(message.content) == 3
    assert message.content[0].type == "text"
    assert message.content[2].type == "image_url"
    assert "data:image/png;base64," in message.model_dump_json()
    assert enforce_context([message], limit=4096, output_tokens=8, visual=visual) > 0

    with pytest.raises(ChatNotFound):
        await visual_message(
            session, uuid.uuid4(), conversation, "What color?", [selection], visual, tmp_path
        )
    with pytest.raises(ChatConflict, match="cannot accept visual"):
        await visual_message(
            session, owner, conversation, "What color?", [selection], None, tmp_path
        )
    (folder / f"{asset_id}.png").write_bytes(b"corrupt")
    with pytest.raises(ChatConflict, match="preview unavailable"):
        await visual_message(
            session, owner, conversation, "What color?", [selection], visual, tmp_path
        )
