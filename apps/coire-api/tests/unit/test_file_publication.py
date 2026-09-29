"""Processed files become visible only after API-owned byte verification."""

from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from coire_api.chat import processing
from coire_api.db import (
    ChatAttachmentRow,
    ChatConversationRow,
    ChatEventRow,
    ChatFileProcessingRow,
    ChatQuotaReservationRow,
    UserRow,
)
from coire_core.models.files import FileProcessAsset, FileProcessRequest, FileProcessResult
from coire_core.settings import Settings

JOB_ID = "01K00000000000000000000000"


class FakeSession:
    def __init__(self, *, image: bool, root: Path) -> None:
        now = datetime.now(UTC)
        owner_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        attachment_id = uuid.uuid4()
        self.owner = UserRow(id=owner_id, email="owner@example.test")
        self.conversation = ChatConversationRow(
            id=conversation_id,
            owner_user_id=owner_id,
            title="Private",
            mode="chat",
            revision=1,
            event_cursor=0,
            created_at=now,
            updated_at=now,
        )
        self.attachment = ChatAttachmentRow(
            id=attachment_id,
            owner_user_id=owner_id,
            conversation_id=conversation_id,
            filename="private.txt",
            detected_type="application/octet-stream",
            original_bytes=5,
            original_sha256="a" * 64,
            original_key=str(attachment_id),
            derived_bytes=0,
            state="processing",
            created_at=now,
            updated_at=now,
        )
        request = FileProcessRequest(
            job_id=JOB_ID,
            input_id=attachment_id,
            source_sha256="a" * 64,
            operation="inspect",
            output_ids=[uuid.uuid4()],
            deadline_at=now + timedelta(seconds=20),
        )
        assets: list[FileProcessAsset] = []
        if image:
            buffer = io.BytesIO()
            Image.new("RGB", (2, 3), (10, 20, 30)).save(buffer, format="PNG")
            data = buffer.getvalue()
            folder = root / JOB_ID
            folder.mkdir()
            (folder / f"{request.output_ids[0]}.png").write_bytes(data)
            assets.append(
                FileProcessAsset(
                    id=request.output_ids[0],
                    sha256=hashlib.sha256(data).hexdigest(),
                    bytes=len(data),
                    media_type="image/png",
                    width=2,
                    height=3,
                )
            )
        result = FileProcessResult(
            job_id=JOB_ID,
            input_id=attachment_id,
            source_sha256="a" * 64,
            detected_type="image/png" if image else "text/plain",
            extracted_text=None if image else "private text",
            assets=assets,
        )
        self.job = ChatFileProcessingRow(
            id=JOB_ID,
            attachment_id=attachment_id,
            owner_user_id=owner_id,
            principal_kind="user",
            principal_subject=str(owner_id),
            request_id=uuid.uuid4(),
            operation="inspect",
            source_key=str(attachment_id),
            source_sha256="a" * 64,
            selected_pages=[],
            output_manifest={
                "request": request.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
            },
            state="processed",
            attempt=1,
            deadline_at=request.deadline_at,
            expires_at=now + timedelta(hours=24),
            created_at=now,
            updated_at=now,
        )
        self.reservation = ChatQuotaReservationRow(
            id=uuid.uuid4(),
            owner_user_id=owner_id,
            conversation_id=conversation_id,
            attachment_id=attachment_id,
            job_id=JOB_ID,
            reserved_bytes=5 + 32 * 1024 * 1024,
            state="active",
            expires_at=now + timedelta(hours=24),
            created_at=now,
        )
        self.events: list[ChatEventRow] = []

    async def get(self, model: object, key: object, **_kwargs: object) -> Any:
        for item in (self.owner, self.conversation, self.attachment, self.job):
            if isinstance(item, cast(type[Any], model)) and item.id == key:
                return item
        return None

    async def scalar(self, _statement: object) -> ChatQuotaReservationRow:
        return self.reservation

    def add(self, item: object) -> None:
        if isinstance(item, ChatEventRow):
            self.events.append(item)


def _wire(monkeypatch: pytest.MonkeyPatch, fake: FakeSession) -> None:
    @asynccontextmanager
    async def fake_scope() -> AsyncIterator[FakeSession]:
        yield fake

    monkeypatch.setattr(processing, "session_scope", fake_scope)


def _settings(root: Path) -> Settings:
    return Settings(chat_derived_root=str(root), _secrets_dir="/nonexistent")  # type: ignore[call-arg]


@pytest.mark.parametrize("image", [False, True])
async def test_publish_verified_file_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, image: bool
) -> None:
    fake = FakeSession(image=image, root=tmp_path)
    _wire(monkeypatch, fake)
    result = await processing.publish_processed_job(JOB_ID, _settings(tmp_path))
    assert result == "ready"
    assert fake.job.state == fake.attachment.state == "ready"
    assert fake.attachment.detected_type == ("image/png" if image else "text/plain")
    assert fake.reservation.reserved_bytes == 5 + fake.attachment.derived_bytes
    assert fake.conversation.revision == 2
    assert len(fake.events) == 1
    assert await processing.publish_processed_job(JOB_ID, _settings(tmp_path)) == "unchanged"
    assert len(fake.events) == 1


@pytest.mark.parametrize("fault", ["altered", "missing", "symlink", "dimensions"])
async def test_invalid_asset_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str
) -> None:
    fake = FakeSession(image=True, root=tmp_path)
    _wire(monkeypatch, fake)
    manifest = cast(dict[str, Any], fake.job.output_manifest)
    result = FileProcessResult.model_validate(manifest["result"])
    path = tmp_path / JOB_ID / f"{result.assets[0].id}.png"
    if fault == "altered":
        path.write_bytes(path.read_bytes()[:-1] + b"x")
    elif fault == "missing":
        path.unlink()
    elif fault == "symlink":
        path.unlink()
        path.symlink_to(tmp_path / "elsewhere")
    else:
        changed = result.model_copy(
            update={"assets": [result.assets[0].model_copy(update={"width": 7})]}
        )
        manifest["result"] = changed.model_dump(mode="json")
    assert await processing.publish_processed_job(JOB_ID, _settings(tmp_path)) == "failed"
    assert fake.attachment.state == fake.job.state == "failed"
    assert fake.job.safe_error == "invalid_worker_output"
    assert fake.reservation.reserved_bytes == 5 + 32 * 1024 * 1024
    assert len(fake.events) == 1


async def test_deleted_parent_never_publishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeSession(image=False, root=tmp_path)
    fake.conversation.deleted_at = datetime.now(UTC)
    _wire(monkeypatch, fake)
    assert await processing.publish_processed_job(JOB_ID, _settings(tmp_path)) == "cancelled"
    assert fake.attachment.state == "processing"
    assert fake.job.state == "cancelled"
    assert not fake.events


async def test_pdf_text_publication_records_page_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeSession(image=False, root=tmp_path)
    manifest = cast(dict[str, Any], fake.job.output_manifest)
    result = FileProcessResult.model_validate(manifest["result"])
    manifest["result"] = result.model_copy(
        update={
            "detected_type": "application/pdf",
            "page_count": 2,
            "extracted_text": "[Page 1]\nHello\n[Page 2]\n",
        }
    ).model_dump(mode="json")
    _wire(monkeypatch, fake)
    assert await processing.publish_processed_job(JOB_ID, _settings(tmp_path)) == "ready"
    assert fake.attachment.page_count == 2
    assert fake.attachment.extraction_status == "complete"


async def test_owner_identity_mismatch_fails_before_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeSession(image=False, root=tmp_path)
    fake.job.owner_user_id = uuid.uuid4()
    _wire(monkeypatch, fake)
    assert await processing.publish_processed_job(JOB_ID, _settings(tmp_path)) == "failed"
    assert fake.attachment.state == "failed"
    assert fake.reservation.reserved_bytes == 5 + 32 * 1024 * 1024
