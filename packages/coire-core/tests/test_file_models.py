"""Contracts for private attachment metadata and isolated processor jobs."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from coire_core.models.files import (
    ChatAttachment,
    ChatAttachmentSelection,
    ChatFileDeleteRequest,
    ChatFileProcessRequest,
    ChatUploadMetadata,
    FileProcessJob,
    FileProcessRequest,
    FileProcessResult,
)


def test_visual_selection_allows_images_but_bounds_pdf_pages() -> None:
    selection = ChatAttachmentSelection(file_id=uuid4(), mode="visual", pages=[1, 3])
    assert selection.pages == [1, 3]
    assert ChatAttachmentSelection(file_id=uuid4(), mode="visual", pages=[]).pages == []
    with pytest.raises(ValidationError):
        ChatAttachmentSelection(file_id=uuid4(), mode="visual", pages=[2, 2])
    with pytest.raises(ValidationError):
        ChatAttachmentSelection(file_id=uuid4(), mode="text", pages=[1])


def test_worker_job_uses_ulid_digest_and_generated_ids_only() -> None:
    job = FileProcessRequest(
        job_id="01JZ6F7Y6CFWPDKBSAADRS6F3Z",
        input_id=uuid4(),
        source_sha256="a" * 64,
        operation="inspect",
        deadline_at=datetime.now(UTC),
    )
    assert job.operation == "inspect"
    with pytest.raises(ValidationError):
        FileProcessRequest.model_validate({**job.model_dump(), "source_path": "/etc/passwd"})
    with pytest.raises(ValidationError):
        FileProcessRequest.model_validate({**job.model_dump(), "job_id": str(uuid4())})
    with pytest.raises(ValidationError):
        FileProcessResult(job_id=job.job_id, input_id=job.input_id, source_sha256="x", assets=[])
    metadata = FileProcessJob(
        id=job.job_id,
        principal_kind="user",
        principal_subject="member-1",
        operation="inspect",
        source_sha256=job.source_sha256,
        state="queued",
        attempt=1,
        deadline_at=job.deadline_at,
        expires_at=job.deadline_at,
    )
    assert metadata.id == job.job_id
    with pytest.raises(ValidationError):
        FileProcessJob.model_validate({**metadata.model_dump(), "attempt": 3})


def test_attachment_has_safe_metadata_and_byte_bounds() -> None:
    attachment = ChatAttachment(
        id=uuid4(),
        owner_id=uuid4(),
        conversation_id=uuid4(),
        filename="notes.txt",
        detected_type="text/plain",
        original_bytes=20,
        original_sha256="b" * 64,
        state="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert attachment.filename == "notes.txt"
    with pytest.raises(ValidationError):
        ChatAttachment.model_validate({**attachment.model_dump(), "filename": "../secret"})


def test_upload_and_explicit_process_metadata_are_strict() -> None:
    upload = ChatUploadMetadata(filename="diagram.png", expected_revision=2)
    assert upload.filename == "diagram.png"
    assert ChatFileDeleteRequest(expected_revision=1).expected_revision == 1
    with pytest.raises(ValidationError):
        ChatUploadMetadata(filename="../diagram.png", expected_revision=2)
    with pytest.raises(ValidationError):
        ChatUploadMetadata.model_validate({**upload.model_dump(), "storage_path": "/etc/passwd"})
    with pytest.raises(ValidationError):
        ChatUploadMetadata(filename="x", expected_revision=0)
    render = ChatFileProcessRequest(
        request_id=uuid4(), expected_revision=2, operation="render", selected_pages=[2, 1]
    )
    assert render.selected_pages == [2, 1]
    with pytest.raises(ValidationError):
        ChatFileProcessRequest(request_id=uuid4(), expected_revision=1, operation="render")
    with pytest.raises(ValidationError):
        ChatFileProcessRequest(
            request_id=uuid4(), expected_revision=1, operation="render", selected_pages=[1, 1]
        )


def test_worker_rejects_duplicate_output_ids() -> None:
    asset_id = uuid4()
    with pytest.raises(ValidationError):
        FileProcessRequest(
            job_id="01JZ6F7Y6CFWPDKBSAADRS6F3Z",
            input_id=uuid4(),
            source_sha256="a" * 64,
            operation="render",
            selected_pages=[1, 2],
            output_ids=[asset_id, asset_id],
            deadline_at=datetime.now(UTC),
        )
