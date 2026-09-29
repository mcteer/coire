"""Deleted files wait for worker erasure before original and row cleanup."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coire_api.chat import maintenance
from coire_api.db import ChatAttachmentRow, ChatConversationRow, ChatFileProcessingRow
from coire_api.file_worker_client import FileWorkerBusy
from coire_core.models.files import FilePurgeResult
from coire_core.settings import Settings
from coire_scheduler import files as scheduler_files

JOB_ID = "01K00000000000000000000000"


class FileRows:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        owner_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        attachment_id = uuid.uuid4()
        self.conversation = ChatConversationRow(
            id=conversation_id,
            owner_user_id=owner_id,
            title="Deleted",
            mode="chat",
            revision=2,
            event_cursor=1,
            deleted_at=now - timedelta(minutes=10),
            created_at=now,
            updated_at=now,
        )
        self.attachment = ChatAttachmentRow(
            id=attachment_id,
            owner_user_id=owner_id,
            conversation_id=conversation_id,
            filename="private.txt",
            detected_type="text/plain",
            original_bytes=5,
            original_sha256="a" * 64,
            original_key=str(attachment_id),
            derived_bytes=0,
            state="ready",
            created_at=now,
            updated_at=now,
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
            output_manifest={"private": "manifest"},
            state="ready",
            attempt=1,
            deadline_at=now - timedelta(minutes=8),
            expires_at=now + timedelta(hours=24),
            created_at=now,
            updated_at=now,
        )
        self.commands: list[str] = []
        self.deleted = False

    async def get(self, model: object, key: object, **_kwargs: object) -> Any:
        if model is ChatConversationRow and key == self.conversation.id:
            return self.conversation
        if model is ChatAttachmentRow and key == self.attachment.id and not self.deleted:
            return self.attachment
        if model is ChatFileProcessingRow and key == self.job.id:
            return self.job
        return None

    async def execute(self, statement: object) -> SimpleNamespace:
        sql = str(statement)
        self.commands.append(sql)
        if sql.startswith("SELECT chat_attachments.id"):
            values: list[object] = (
                [self.attachment.id] if self.job.state == "purged" and not self.deleted else []
            )
        elif sql.startswith("SELECT") and "chat_file_processing" in sql and "JOIN" in sql:
            values = [self.job.id] if self.job.state != "purged" else []
        elif (
            sql.startswith("SELECT chat_file_processing.id") and "FROM chat_file_processing" in sql
        ):
            values = (
                [self.job.id]
                if self.job.state == "failed"
                and (self.job.output_manifest or {}).get("output_purged") is not True
                else []
            )
        elif sql.startswith("SELECT") and "chat_file_processing" in sql:
            values = [self.job]
        elif sql.startswith("SELECT"):
            values = [] if self.deleted else [self.attachment.id]
        else:
            values = []
        return SimpleNamespace(scalars=lambda: values)

    async def delete(self, _row: object) -> None:
        self.deleted = True


def _settings(root: Path) -> Settings:
    return Settings(chat_original_root=str(root), _secrets_dir="/nonexistent")  # type: ignore[call-arg]


async def test_scheduler_retries_idempotent_worker_purge_before_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = FileRows()
    calls: list[str] = []

    @asynccontextmanager
    async def sessions() -> AsyncIterator[FileRows]:
        yield rows

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def purge(self, job_id: str) -> FilePurgeResult:
            assert rows.job.state == "purging"
            calls.append(job_id)
            if len(calls) == 1:
                raise FileWorkerBusy("active")
            return FilePurgeResult(job_id=job_id)

    monkeypatch.setattr(scheduler_files, "session_scope", sessions)
    monkeypatch.setattr(scheduler_files, "FileWorkerClient", lambda _settings: Client())
    assert await scheduler_files.purge_deleted_file_outputs(_settings(tmp_path)) == 0
    assert rows.job.state == "purging"
    before = rows.job.output_manifest
    assert before is not None
    assert await scheduler_files.purge_deleted_file_outputs(_settings(tmp_path)) == 1
    assert rows.job.state == "purged"
    assert rows.job.output_manifest is None
    assert calls == [JOB_ID, JOB_ID]


async def test_api_waits_for_derived_marker_then_erases_original_and_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = FileRows()
    original = tmp_path / str(rows.attachment.id)
    original.write_bytes(b"private")

    @asynccontextmanager
    async def sessions() -> AsyncIterator[FileRows]:
        yield rows

    monkeypatch.setattr(maintenance, "session_scope", sessions)
    assert await maintenance.purge_deleted_files(_settings(tmp_path)) == 0
    assert original.exists()
    rows.job.state = "purged"
    assert await maintenance.purge_deleted_files(_settings(tmp_path)) == 1
    assert not original.exists()
    assert rows.deleted
    assert sum(command.startswith("DELETE FROM chat_") for command in rows.commands) == 2
    assert await maintenance.purge_deleted_files(_settings(tmp_path)) == 0


async def test_failed_output_cleanup_preserves_visible_failure_and_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = FileRows()
    rows.job.state = "failed"
    rows.job.safe_error = "worker_status_missing"
    rows.job.output_manifest = {"client_request": {"expected_revision": 3}}
    calls: list[str] = []

    @asynccontextmanager
    async def sessions() -> AsyncIterator[FileRows]:
        yield rows

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def purge(self, job_id: str) -> FilePurgeResult:
            calls.append(job_id)
            if len(calls) == 1:
                raise FileWorkerBusy("active")
            return FilePurgeResult(job_id=job_id)

    monkeypatch.setattr(scheduler_files, "session_scope", sessions)
    monkeypatch.setattr(scheduler_files, "FileWorkerClient", lambda _settings: Client())
    assert await scheduler_files.purge_failed_file_outputs(_settings(tmp_path)) == 0
    assert rows.job.state == "failed"
    assert rows.job.safe_error == "worker_status_missing"
    assert await scheduler_files.purge_failed_file_outputs(_settings(tmp_path)) == 1
    assert rows.job.output_manifest == {
        "output_purged": True,
        "client_request": {"expected_revision": 3},
    }
    assert rows.job.state == "failed"
    assert rows.job.attempt == 1
    assert await scheduler_files.purge_failed_file_outputs(_settings(tmp_path)) == 0
    assert calls == [JOB_ID, JOB_ID]


def test_stale_staging_sweep_is_generated_and_age_bounded(tmp_path: Path) -> None:
    old = tmp_path / f".{uuid.uuid4()}.{uuid.uuid4()}.uploading"
    fresh = tmp_path / f".{uuid.uuid4()}.{uuid.uuid4()}.uploading"
    unrelated = tmp_path / "not-generated.uploading"
    old.write_bytes(b"old")
    fresh.write_bytes(b"new")
    unrelated.write_bytes(b"keep")
    old_time = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(old, (old_time, old_time))
    assert maintenance.purge_stale_uploads(tmp_path, limit=1) == 1
    assert not old.exists()
    assert fresh.exists()
    assert unrelated.exists()
