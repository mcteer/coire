"""File dispatch commits a request before POST and never rerenders on recovery."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from coire_api.chat.file_manifest import validate_result
from coire_api.db import ChatAttachmentRow, ChatConversationRow, ChatFileProcessingRow
from coire_api.file_worker_client import FileWorkerBusy, FileWorkerError, FileWorkerMissing
from coire_core.models.files import (
    FileProcessAsset,
    FileProcessRequest,
    FileProcessResult,
    FileProcessStatus,
)
from coire_core.settings import Settings
from coire_scheduler import files

JOB_ID = "01K00000000000000000000000"


class FakeSession:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        owner_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        attachment_id = uuid.uuid4()
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
            filename="hidden.txt",
            detected_type="application/octet-stream",
            original_bytes=5,
            original_sha256="a" * 64,
            original_key=str(attachment_id),
            derived_bytes=0,
            state="processing",
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
            state="queued",
            attempt=1,
            deadline_at=now + timedelta(seconds=20),
            expires_at=now + timedelta(hours=24),
            created_at=now,
            updated_at=now,
        )
        self.commits = 0

    async def get(self, model: object, key: object, **_kwargs: object) -> Any:
        if model is ChatFileProcessingRow and key == self.job.id:
            return self.job
        if model is ChatAttachmentRow and key == self.attachment.id:
            return self.attachment
        if model is ChatConversationRow and key == self.conversation.id:
            return self.conversation
        return None


class FakeClient:
    def __init__(
        self,
        session: FakeSession,
        *,
        busy_once: bool = False,
        missing: bool = False,
        mismatch: bool = False,
    ) -> None:
        self.session = session
        self.busy_once = busy_once
        self.missing = missing
        self.mismatch = mismatch
        self.calls: list[str] = []
        self.request: FileProcessRequest | None = None

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    def _processed(self) -> FileProcessStatus:
        assert self.request is not None
        result = FileProcessResult(
            job_id=JOB_ID,
            input_id=uuid.uuid4() if self.mismatch else self.request.input_id,
            source_sha256=self.request.source_sha256,
            detected_type="text/plain",
            extracted_text="private content",
            assets=[],
        )
        return FileProcessStatus(
            job_id=JOB_ID, state="processed", result=result, updated_at=datetime.now(UTC)
        )

    async def process(self, request: FileProcessRequest) -> FileProcessStatus:
        assert self.session.commits >= 1, "running request must commit before worker POST"
        self.calls.append("process")
        self.request = request
        if self.busy_once:
            self.busy_once = False
            raise FileWorkerBusy("worker busy")
        if self.missing:
            raise FileWorkerError("ambiguous submit")
        return self._processed()

    async def status(self, job_id: str) -> FileProcessStatus:
        assert job_id == JOB_ID
        self.calls.append("status")
        if self.missing:
            raise FileWorkerMissing("missing")
        return self._processed()

    async def cancel(self, job_id: str) -> FileProcessStatus:
        assert job_id == JOB_ID
        self.calls.append("cancel")
        return FileProcessStatus(job_id=JOB_ID, state="cancelled", updated_at=datetime.now(UTC))


def _wire(monkeypatch: pytest.MonkeyPatch, session: FakeSession, client: FakeClient) -> None:
    @asynccontextmanager
    async def fake_scope() -> AsyncIterator[FakeSession]:
        yield session
        session.commits += 1

    monkeypatch.setattr(files, "session_scope", fake_scope)
    monkeypatch.setattr(files, "FileWorkerClient", lambda _settings: client)


def _settings() -> Settings:
    return Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]


async def test_queued_request_commits_before_worker_post(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    client = FakeClient(session)
    _wire(monkeypatch, session, client)
    await files.drive_file_job(JOB_ID, _settings())
    assert client.calls == ["process"]
    assert session.job.state == "processed"
    assert session.job.output_manifest is not None
    assert (
        cast(dict[str, Any], session.job.output_manifest["result"])["extracted_text"]
        == "private content"
    )
    assert session.attachment.state == "processing"  # API verifies/publishes later.


async def test_temporary_request_uses_generated_source_and_recovers_by_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    session.job.attachment_id = None
    source_id = uuid.uuid4()
    session.job.source_key = str(source_id)
    client = FakeClient(session)
    _wire(monkeypatch, session, client)
    await files.drive_file_job(JOB_ID, _settings())
    assert client.calls == ["process"]
    assert client.request is not None and client.request.input_id == source_id
    assert session.job.state == "processed"
    assert session.attachment.state == "processing"
    session.job.state = "running"
    client.calls.clear()
    await files.drive_file_job(JOB_ID, _settings())
    assert client.calls == ["status"]
    assert session.job.state == "processed"


async def test_expired_temporary_request_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    session.job.attachment_id = None
    session.job.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    client = FakeClient(session)
    _wire(monkeypatch, session, client)
    await files.drive_file_job(JOB_ID, _settings())
    assert session.job.state == "cancelled"
    assert client.calls == ["cancel"]


async def test_running_recovery_queries_status_only(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    session.job.state = "running"
    request = FileProcessRequest(
        job_id=JOB_ID,
        input_id=session.attachment.id,
        source_sha256=session.attachment.original_sha256,
        operation="inspect",
        output_ids=[uuid.uuid4()],
        deadline_at=datetime.now(UTC) + timedelta(seconds=20),
    )
    session.job.output_manifest = {"request": request.model_dump(mode="json")}
    client = FakeClient(session)
    client.request = request
    _wire(monkeypatch, session, client)
    await files.drive_file_job(JOB_ID, _settings())
    assert client.calls == ["status"]
    assert session.job.state == "processed"


async def test_retry_revision_binding_survives_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    session.job.output_manifest = {"client_request": {"expected_revision": 3}}
    client = FakeClient(session)
    _wire(monkeypatch, session, client)
    await files.drive_file_job(JOB_ID, _settings())
    assert session.job.state == "processed"
    assert session.job.output_manifest is not None
    assert session.job.output_manifest["client_request"] == {"expected_revision": 3}


@pytest.mark.parametrize(
    "busy,missing,mismatch,expected",
    [
        (True, False, False, "processed"),
        (False, True, False, "failed"),
        (False, False, True, "failed"),
    ],
)
async def test_busy_missing_and_manifest_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    busy: bool,
    missing: bool,
    mismatch: bool,
    expected: str,
) -> None:
    session = FakeSession()
    client = FakeClient(session, busy_once=busy, missing=missing, mismatch=mismatch)
    _wire(monkeypatch, session, client)
    if busy:

        async def no_sleep(_delay: float) -> None:
            pass

        monkeypatch.setattr("coire_scheduler.files.asyncio.sleep", no_sleep)
    await files.drive_file_job(JOB_ID, _settings())
    assert session.job.state == expected
    assert client.calls == (
        ["process", "process"] if busy else ["process", "status"] if missing else ["process"]
    )
    if mismatch:
        assert session.job.safe_error == "invalid_worker_manifest"


async def test_deleted_attachment_cancels_without_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    session.conversation.deleted_at = datetime.now(UTC)
    client = FakeClient(session)
    _wire(monkeypatch, session, client)
    await files.drive_file_job(JOB_ID, _settings())
    assert session.job.state == "cancelled"
    assert client.calls == ["cancel"]


async def test_source_identity_mismatch_fails_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    session.job.source_sha256 = "b" * 64
    client = FakeClient(session)
    _wire(monkeypatch, session, client)
    await files.drive_file_job(JOB_ID, _settings())
    assert session.job.state == "failed"
    assert session.attachment.state == "failed"
    assert session.job.safe_error == "source_identity_mismatch"
    assert client.calls == []


async def test_deletion_during_processing_cancels_late_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()

    class DeleteDuringProcess(FakeClient):
        async def process(self, request: FileProcessRequest) -> FileProcessStatus:
            self.calls.append("process")
            self.request = request
            session.conversation.deleted_at = datetime.now(UTC)
            return FileProcessStatus(job_id=JOB_ID, state="running", updated_at=datetime.now(UTC))

    client = DeleteDuringProcess(session)
    _wire(monkeypatch, session, client)
    await files.drive_file_job(JOB_ID, _settings())
    assert session.job.state == "cancelled"
    assert client.calls == ["process", "cancel"]
    assert session.job.output_manifest is not None
    assert "result" not in session.job.output_manifest


def test_visual_manifest_requires_reserved_id_and_page() -> None:
    request = FileProcessRequest(
        job_id=JOB_ID,
        input_id=uuid.uuid4(),
        source_sha256="a" * 64,
        operation="render",
        selected_pages=[2],
        output_ids=[uuid.uuid4()],
        deadline_at=datetime.now(UTC) + timedelta(seconds=20),
    )
    asset = FileProcessAsset(
        id=uuid.uuid4(),
        sha256="b" * 64,
        bytes=10,
        media_type="image/png",
        width=10,
        height=10,
        page=2,
    )
    result = FileProcessResult(
        job_id=JOB_ID,
        input_id=request.input_id,
        source_sha256=request.source_sha256,
        detected_type="application/pdf",
        page_count=2,
        assets=[asset],
    )
    with pytest.raises(ValueError, match="render manifest mismatch"):
        validate_result(request, result)
    valid = result.model_copy(
        update={"assets": [asset.model_copy(update={"id": request.output_ids[0]})]}
    )
    validate_result(request, valid)
    with pytest.raises(ValueError, match="render manifest mismatch"):
        validate_result(
            request,
            valid.model_copy(
                update={"assets": [valid.assets[0].model_copy(update={"media_type": "image/jpeg"})]}
            ),
        )
