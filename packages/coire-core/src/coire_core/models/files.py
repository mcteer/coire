"""Private attachment and isolated file-worker wire contracts."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ULID_PATTERN = r"^[0-9A-HJKMNP-TV-Z]{26}$"
SHA256_PATTERN = r"^[a-f0-9]{64}$"


class ChatAttachmentSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    mode: Literal["text", "visual"]
    pages: list[int] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def check_pages(self) -> ChatAttachmentSelection:
        if self.mode == "text" and self.pages:
            raise ValueError("text selection cannot specify visual pages")
        if len(set(self.pages)) != len(self.pages) or any(
            page < 1 or page > 50 for page in self.pages
        ):
            raise ValueError("pages must be unique and within 1..50")
        return self


class ChatAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    owner_id: uuid.UUID
    conversation_id: uuid.UUID
    filename: str = Field(min_length=1, max_length=255)
    detected_type: str = Field(min_length=1, max_length=100)
    original_bytes: int = Field(ge=1, le=10 * 1024 * 1024)
    original_sha256: str = Field(pattern=SHA256_PATTERN)
    derived_bytes: int = Field(default=0, ge=0, le=32 * 1024 * 1024)
    state: Literal["uploading", "processing", "ready", "failed", "deleting"]
    page_count: int | None = Field(default=None, ge=1, le=50)
    safe_error: str | None = Field(default=None, max_length=500)
    created_at: datetime
    updated_at: datetime

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if (
            value in {".", ".."}
            or ".." in value
            or "/" in value
            or "\\" in value
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("filename must be a safe basename")
        return value


class FileProcessRequest(BaseModel):
    """Worker inputs use generated IDs, never client paths or URLs."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(pattern=ULID_PATTERN)
    input_id: uuid.UUID
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    operation: Literal["inspect", "render"]
    selected_pages: list[int] = Field(default_factory=list, max_length=10)
    output_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    deadline_at: datetime

    @model_validator(mode="after")
    def check_selection(self) -> FileProcessRequest:
        if any(page < 1 or page > 50 for page in self.selected_pages):
            raise ValueError("pages must be within 1..50")
        if len(set(self.selected_pages)) != len(self.selected_pages):
            raise ValueError("pages must be unique")
        if self.operation == "render" and not self.selected_pages:
            raise ValueError("render requires selected pages")
        if self.operation == "render" and len(self.output_ids) != len(self.selected_pages):
            raise ValueError("output IDs must match selected pages")
        return self


class FileProcessAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    sha256: str = Field(pattern=SHA256_PATTERN)
    bytes: int = Field(ge=1, le=32 * 1024 * 1024)
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    width: int = Field(ge=1, le=2048)
    height: int = Field(ge=1, le=2048)
    page: int | None = Field(default=None, ge=1, le=50)


class FileProcessResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(pattern=ULID_PATTERN)
    input_id: uuid.UUID
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    detected_type: str | None = Field(default=None, max_length=100)
    page_count: int | None = Field(default=None, ge=1, le=50)
    extracted_text: str | None = Field(default=None, max_length=1024 * 1024)
    assets: list[FileProcessAsset] = Field(max_length=10)

    @model_validator(mode="after")
    def bounded_output(self) -> FileProcessResult:
        if sum(asset.bytes for asset in self.assets) > 32 * 1024 * 1024:
            raise ValueError("derived output exceeds 32 MiB")
        if (
            self.extracted_text is not None
            and len(self.extracted_text.encode("utf-8")) > 1024 * 1024
        ):
            raise ValueError("extracted text exceeds 1 MiB")
        return self


class FileProcessStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(pattern=ULID_PATTERN)
    state: Literal["queued", "running", "processed", "ready", "failed", "cancelled"]
    result: FileProcessResult | None = None
    safe_error: str | None = Field(default=None, max_length=500)
    updated_at: datetime

    @model_validator(mode="after")
    def result_matches_job(self) -> FileProcessStatus:
        if self.result is not None and self.result.job_id != self.job_id:
            raise ValueError("result job ID mismatch")
        return self


class FileProcessJob(BaseModel):
    """Durable scheduler-facing job metadata without private storage paths."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ULID_PATTERN)
    attachment_id: uuid.UUID | None = None
    principal_kind: Literal["user", "api_key", "run"]
    principal_subject: str = Field(min_length=1, max_length=128)
    operation: Literal["inspect", "render"]
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    selected_pages: list[int] = Field(default_factory=list, max_length=10)
    state: Literal["queued", "running", "processed", "ready", "failed", "cancelled"]
    attempt: int = Field(ge=1, le=2)
    deadline_at: datetime
    expires_at: datetime
    safe_error: str | None = Field(default=None, max_length=500)


class FileProcessCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(pattern=ULID_PATTERN)


class FileWorkerHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"


def is_ulid(value: str) -> bool:
    """Whether an identifier follows the repository's ULID wire spelling."""

    return re.fullmatch(ULID_PATTERN, value) is not None
