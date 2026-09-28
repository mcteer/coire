"""Strict application payloads for the three public MCP coding tools."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class McpToolName(StrEnum):
    RESEARCH = "research"
    PLAN = "plan"
    APPLY = "apply"


class McpCallState(StrEnum):
    ACCEPTED = "accepted"
    PREPARING = "preparing"
    QUEUED = "queued"
    RUNNING = "running"
    COLLECTING = "collecting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class TestStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"


class WorkspaceSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_url: HttpUrl | None = None
    workspace_id: uuid.UUID | None = None
    revision: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

    @model_validator(mode="after")
    def one_source(self) -> WorkspaceSource:
        if (self.repository_url is None) == (self.workspace_id is None):
            raise ValueError("exactly one repository_url or workspace_id is required")
        if self.repository_url is not None:
            url = self.repository_url
            if url.scheme != "https" or url.username is not None or url.password is not None:
                raise ValueError("repository URL must be HTTPS without embedded credentials")
        return self


class WorkspaceRegistrationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_url: HttpUrl

    @model_validator(mode="after")
    def https_without_credentials(self) -> WorkspaceRegistrationCreate:
        source = WorkspaceSource(repository_url=self.repository_url, revision="HEAD")
        if source.repository_url != self.repository_url:
            raise ValueError("repository URL is invalid")
        return self


class RegisteredWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    owner_user_id: uuid.UUID
    repository_url: HttpUrl
    created_at: datetime


class ResearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: WorkspaceSource
    question: str = Field(min_length=1, max_length=100_000)
    model_id: uuid.UUID | None = None


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: WorkspaceSource
    goal: str = Field(min_length=1, max_length=100_000)
    research_result_id: uuid.UUID | None = None
    model_id: uuid.UUID | None = None


class ApplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: WorkspaceSource
    plan_result_id: uuid.UUID | None = None
    plan: str | None = Field(default=None, min_length=1, max_length=100_000)
    model_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def has_plan(self) -> ApplyInput:
        if (self.plan_result_id is None) == (self.plan is None):
            raise ValueError("exactly one plan_result_id or plan is required")
        return self


class FileCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    line: int = Field(ge=1)
    excerpt: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def relative_path(self) -> FileCitation:
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or self.path.startswith("./")
            or ".git" in path.parts
            or ".coire" in path.parts
        ):
            raise ValueError("citation path must be repository-relative")
        return self


class ResearchDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=200_000)
    citations: list[FileCitation] = Field(min_length=1, max_length=128)


class ResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: uuid.UUID
    run_id: uuid.UUID
    source_revision: str = Field(min_length=40, max_length=40, pattern=r"^[a-f0-9]{40}$")
    answer: str = Field(min_length=1, max_length=200_000)
    citations: list[FileCitation] = Field(min_length=1, max_length=128)


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=4000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=32)


class PlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=100_000)
    steps: list[PlanStep] = Field(min_length=1, max_length=64)


class ApplyFileEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=131_072)

    @model_validator(mode="after")
    def relative_path(self) -> ApplyFileEdit:
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or self.path.startswith("./")
            or ".git" in path.parts
            or ".coire" in path.parts
        ):
            raise ValueError("edit path must be repository-relative outside control directories")
        return self


class ApplyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=4000)
    edits: list[ApplyFileEdit] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_paths(self) -> ApplyDraft:
        paths = [edit.path for edit in self.edits]
        if len(paths) != len(set(paths)):
            raise ValueError("apply draft contains duplicate paths")
        return self


class PlanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: uuid.UUID
    run_id: uuid.UUID
    source_revision: str = Field(min_length=40, max_length=40, pattern=r"^[a-f0-9]{40}$")
    goal: str = Field(min_length=1, max_length=100_000)
    steps: list[PlanStep] = Field(min_length=1, max_length=64)


class TestSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TestStatus
    command: list[str] = Field(default_factory=list, max_length=32)
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    output_excerpt: str = Field(default="", max_length=16_384)


class ApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    branch: str = Field(pattern=r"^coire/[a-z0-9][a-z0-9-]{0,100}$")
    base_revision: str = Field(min_length=40, max_length=40, pattern=r"^[a-f0-9]{40}$")
    head_revision: str = Field(min_length=40, max_length=40, pattern=r"^[a-f0-9]{40}$")
    diff_excerpt: str = Field(max_length=1_048_576)
    diff_truncated: bool
    tests: TestSummary
    artifact_id: uuid.UUID
    artifact_url: str | None = Field(default=None, max_length=256)


class McpCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    tool: McpToolName
    owner_user_id: uuid.UUID
    credential_id: uuid.UUID | None = None
    source: WorkspaceSource
    model_id: uuid.UUID
    run_id: uuid.UUID | None = None
    state: McpCallState
    failure_code: str | None = Field(default=None, max_length=64)
    requested_at: datetime
    finished_at: datetime | None = None


class BranchArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    owner_user_id: uuid.UUID
    run_id: uuid.UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    expires_at: datetime
    collected_at: datetime
