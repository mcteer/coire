"""Expired temporary images erase worker output before generated originals."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from coire_api.chat import maintenance
from coire_api.db import ChatFileProcessingRow
from coire_api.file_worker_client import FileWorkerBusy
from coire_core.models.files import FilePurgeResult
from coire_core.settings import Settings
from coire_scheduler import files

JOB_ID = "01K00000000000000000000000"


class TemporaryRows:
    def __init__(self) -> None:
        self.source_id = uuid.uuid4()
        now = datetime.now(UTC)
        self.job = ChatFileProcessingRow(
            id=JOB_ID,
            attachment_id=None,
            principal_kind="user",
            principal_subject=str(uuid.uuid4()),
            request_id=uuid.uuid4(),
            operation="inspect",
            source_key=str(self.source_id),
            source_sha256="a" * 64,
            selected_pages=[],
            output_manifest={"private": "manifest"},
            state="ready",
            attempt=1,
            deadline_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
        self.deleted = False

    async def execute(self, _statement: object) -> SimpleNamespace:
        ids = [self.job.id] if not self.deleted else []
        return SimpleNamespace(scalars=lambda: ids)

    async def get(
        self, _model: object, job_id: str, **_kwargs: object
    ) -> ChatFileProcessingRow | None:
        return self.job if job_id == JOB_ID and not self.deleted else None

    async def delete(self, _row: ChatFileProcessingRow) -> None:
        self.deleted = True


async def test_expired_worker_output_retries_busy_then_marks_purged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = TemporaryRows()
    calls = 0

    @asynccontextmanager
    async def sessions() -> AsyncIterator[TemporaryRows]:
        yield rows

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def purge(self, job_id: str) -> FilePurgeResult:
            nonlocal calls
            calls += 1
            assert job_id == JOB_ID
            if calls == 1:
                raise FileWorkerBusy("active")
            return FilePurgeResult(job_id=job_id)

    monkeypatch.setattr(files, "session_scope", sessions)
    monkeypatch.setattr(files, "FileWorkerClient", lambda _settings: Client())
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert await files.purge_expired_temporary_outputs(settings) == 0
    assert rows.job.state == "ready" and rows.job.output_manifest is not None
    assert await files.purge_expired_temporary_outputs(settings) == 1
    assert rows.job.state == "purged" and rows.job.output_manifest is None


async def test_original_erasure_waits_for_worker_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = TemporaryRows()
    original = tmp_path / str(rows.source_id)
    original.write_bytes(b"private image")

    @asynccontextmanager
    async def sessions() -> AsyncIterator[TemporaryRows]:
        yield rows

    monkeypatch.setattr(maintenance, "session_scope", sessions)
    settings = Settings(chat_original_root=str(tmp_path), _secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert await maintenance.purge_expired_temporary_originals(settings) == 0
    assert original.exists()
    rows.job.state = "purged"
    assert await maintenance.purge_expired_temporary_originals(settings) == 1
    assert not original.exists() and rows.deleted
