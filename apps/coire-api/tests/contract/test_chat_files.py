"""Original upload, generated storage, quota and owner-bound download contracts."""

from __future__ import annotations

import asyncio
import io
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import UploadFile
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.chat.files import admit_original, new_job_id, stage_original
from coire_api.db import (
    ChatAttachmentRow,
    ChatConversationRow,
    ChatEventRow,
    ChatFileProcessingRow,
    ChatQuotaReservationRow,
    get_session,
)
from coire_core.errors import ChatConflict, ChatQuotaExceeded
from coire_core.models.files import ChatUploadMetadata, is_ulid
from coire_core.settings import Settings, get_settings


class FileSession:
    def __init__(self) -> None:
        self.owner = uuid.uuid4()
        now = datetime.now(UTC)
        self.conversation = ChatConversationRow(
            id=uuid.uuid4(),
            owner_user_id=self.owner,
            title="Private",
            mode="chat",
            revision=2,
            event_cursor=0,
            created_at=now,
            updated_at=now,
        )
        self.rows: list[object] = []
        self.used = 0
        self.queries: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_commit = False

    async def scalar(self, statement: object) -> object:
        query = str(statement)
        self.queries.append(query)
        if "FROM users" in query:
            return object()
        if "FROM chat_conversations" in query:
            return self.conversation
        if "FROM chat_quota_reservations" in query:
            return self.used
        raise AssertionError(query)

    async def get(self, model: object, identifier: uuid.UUID) -> object | None:
        if model is ChatConversationRow and identifier == self.conversation.id:
            return self.conversation
        if model is ChatAttachmentRow:
            return next(
                (
                    row
                    for row in self.rows
                    if isinstance(row, ChatAttachmentRow) and row.id == identifier
                ),
                None,
            )
        return None

    def add(self, row: object) -> None:
        self.rows.append(row)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError("database unavailable")

    async def rollback(self) -> None:
        self.rollbacks += 1


def _settings(root: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        chat_enabled=True,
        chat_browser_origin="http://localhost",
        chat_original_root=str(root),
        identity_legacy_admin_enabled=True,
        admin_token=SecretStr("file-test"),
    )


async def test_generated_atomic_stage_and_limits(tmp_path: Path) -> None:
    original = await stage_original(UploadFile(file=io.BytesIO(b"private")), tmp_path, 10)
    assert original.size == 7
    assert original.id.hex not in "private"
    assert not original.target.exists()
    original.publish()
    assert original.target.read_bytes() == b"private"
    assert not original.temporary.exists()
    with pytest.raises(ChatQuotaExceeded):
        await stage_original(UploadFile(file=io.BytesIO(b"too long")), tmp_path, 4)
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.uploading")))
    assert is_ulid(new_job_id())


async def test_upload_reserves_under_owner_lock_and_records_job(tmp_path: Path) -> None:
    session = FileSession()
    settings = _settings(tmp_path)
    staged = await stage_original(UploadFile(file=io.BytesIO(b"secret bytes")), tmp_path, 100)
    response = await admit_original(
        cast(AsyncSession, session),
        Principal(kind=PrincipalKind.USER, user_id=session.owner),
        session.conversation.id,
        ChatUploadMetadata(filename="memo.txt", expected_revision=2),
        staged,
        settings,
    )
    assert response.id == staged.id
    assert response.state == "processing"
    assert staged.target.read_bytes() == b"secret bytes"
    assert session.conversation.revision == 3
    assert session.commits == 1
    assert "FOR UPDATE" in session.queries[0]
    assert "FOR UPDATE" in session.queries[1]
    job = next(row for row in session.rows if isinstance(row, ChatFileProcessingRow))
    reservation = next(row for row in session.rows if isinstance(row, ChatQuotaReservationRow))
    event = next(row for row in session.rows if isinstance(row, ChatEventRow))
    assert is_ulid(job.id)
    assert job.source_key == str(response.id)
    assert job.source_sha256 == response.original_sha256
    assert reservation.reserved_bytes == response.original_bytes + 32 * 1024 * 1024
    assert event.type == "attachment.changed"
    assert "secret bytes" not in str(event.payload)


@pytest.mark.parametrize(
    "revision,used,error", [(1, 0, ChatConflict), (2, 49 * 1024 * 1024, ChatQuotaExceeded)]
)
async def test_refused_upload_discards_staging(
    tmp_path: Path, revision: int, used: int, error: type[Exception]
) -> None:
    session = FileSession()
    session.used = used
    staged = await stage_original(UploadFile(file=io.BytesIO(b"secret bytes")), tmp_path, 100)
    with pytest.raises(error):
        await admit_original(
            cast(AsyncSession, session),
            Principal(kind=PrincipalKind.USER, user_id=session.owner),
            session.conversation.id,
            ChatUploadMetadata(filename="memo.txt", expected_revision=revision),
            staged,
            _settings(tmp_path),
        )
    assert session.rollbacks == 1
    assert not staged.temporary.exists()
    assert not staged.target.exists()
    assert not session.rows


async def test_commit_failure_removes_published_original(tmp_path: Path) -> None:
    session = FileSession()
    session.fail_commit = True
    staged = await stage_original(UploadFile(file=io.BytesIO(b"private")), tmp_path, 100)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await admit_original(
            cast(AsyncSession, session),
            Principal(kind=PrincipalKind.USER, user_id=session.owner),
            session.conversation.id,
            ChatUploadMetadata(filename="memo.txt", expected_revision=2),
            staged,
            _settings(tmp_path),
        )
    assert session.rollbacks == 1
    assert not staged.target.exists()


async def test_routes_reauthorize_upload_and_download(tmp_path: Path) -> None:
    session = FileSession()
    settings = _settings(tmp_path)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[require_principal] = lambda: Principal(
        kind=PrincipalKind.USER, user_id=session.owner
    )

    async def fake_session() -> AsyncIterator[FileSession]:
        yield session

    app.dependency_overrides[get_session] = fake_session
    path = f"/api/v1/chat/conversations/{session.conversation.id}/files"
    multipart = {"file": ("ignored-name.txt", b"private body", "text/plain")}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"Authorization": "Bearer file-test"},
    ) as client:
        form = {"filename": "note.txt", "expected_revision": "2"}
        refused = await client.post(path, data=form, files=multipart)
        assert refused.status_code == 403
        accepted = await client.post(
            path,
            data=form,
            files=multipart,
            headers={"Origin": "http://localhost"},
        )
        assert accepted.status_code == 202, accepted.text
        file_id = accepted.json()["id"]
        assert accepted.json()["filename"] == "note.txt"
        assert (tmp_path / file_id).read_bytes() == b"private body"
        metadata = await client.get(f"{path}/{file_id}")
        assert metadata.status_code == 200
        downloaded = await client.get(f"{path}/{file_id}/content")
        assert downloaded.status_code == 200
        assert downloaded.content == b"private body"
        assert downloaded.headers["content-disposition"].startswith("attachment;")
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert downloaded.headers["cache-control"] == "no-store"
        foreign = await client.get(f"{path}/{uuid.uuid4()}/content")
        assert foreign.status_code == 404
        cross_parent = await client.get(
            f"/api/v1/chat/conversations/{uuid.uuid4()}/files/{file_id}/content"
        )
        assert cross_parent.status_code == 404
        app.dependency_overrides[require_principal] = lambda: Principal(
            kind=PrincipalKind.USER, user_id=uuid.uuid4()
        )
        denied_upload = await client.post(
            path,
            data={"filename": "foreign.txt", "expected_revision": "3"},
            files=multipart,
            headers={"Origin": "http://localhost"},
        )
        assert denied_upload.status_code == 404
        assert len(await asyncio.to_thread(lambda: list(tmp_path.iterdir()))) == 1
        app.dependency_overrides[require_principal] = lambda: Principal(
            kind=PrincipalKind.USER, user_id=session.owner
        )
        original = tmp_path / file_id
        moved = tmp_path / "moved-original"
        original.rename(moved)
        original.symlink_to(moved)
        assert (await client.get(f"{path}/{file_id}/content")).status_code == 404
        session.conversation.deleted_at = datetime.now(UTC)
        assert (await client.get(f"{path}/{file_id}")).status_code == 404
