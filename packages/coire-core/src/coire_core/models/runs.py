"""Strict contracts for ephemeral Studio container runs."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coire_core.models.harness import ProfileName


class AgentRunState(StrEnum):
    QUEUED = "queued"
    PLACING = "placing"
    CREATING = "creating"
    RUNNING = "running"
    COLLECTING = "collecting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RESULT_COLLECTION_FAILED = "result_collection_failed"
    TIMED_OUT = "timed_out"
    KILL_REQUESTED = "kill_requested"
    KILLED = "killed"


TERMINAL_RUN_STATES = frozenset(
    {
        AgentRunState.SUCCEEDED,
        AgentRunState.FAILED,
        AgentRunState.RESULT_COLLECTION_FAILED,
        AgentRunState.TIMED_OUT,
        AgentRunState.KILLED,
    }
)


class RunOperation(StrEnum):
    CREATE = "create"
    START = "start"
    LOGS = "logs"
    WAIT = "wait"
    COLLECT = "collect"
    REMOVE = "remove"
    KILL = "kill"
    RECONCILE = "reconcile"


class RunCommandState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_bytes: int = Field(default=4 * 1024**3, ge=128 * 1024**2, le=16 * 1024**3)
    nano_cpus: int = Field(default=2_000_000_000, ge=100_000_000, le=16_000_000_000)
    pids_limit: int = Field(default=256, ge=16, le=4096)
    timeout_seconds: int = Field(default=900, ge=10, le=86_400)
    log_bytes: int = Field(default=8 * 1024**2, ge=1024, le=128 * 1024**2)
    result_bytes: int = Field(default=4 * 1024**2, ge=1024, le=64 * 1024**2)


class RunTokenScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    permitted_model_ids: frozenset[uuid.UUID] = Field(min_length=1, max_length=16)
    permitted_tools: frozenset[str] = Field(default_factory=frozenset, max_length=10)
    spend_limit_tokens: int = Field(ge=1, le=100_000_000)


class AgentRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: ProfileName
    primary_model_id: uuid.UUID
    workspace_ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    permitted_model_ids: frozenset[uuid.UUID] = Field(min_length=1, max_length=16)
    permitted_tools: frozenset[str] = Field(default_factory=frozenset, max_length=10)
    spend_limit_tokens: int = Field(default=100_000, ge=1, le=100_000_000)
    limits: RunLimits = Field(default_factory=RunLimits)

    @model_validator(mode="after")
    def primary_model_must_be_permitted(self) -> AgentRunCreate:
        if self.primary_model_id not in self.permitted_model_ids:
            raise ValueError("primary_model_id must be in permitted_model_ids")
        return self


class RunResourceUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    peak_memory_bytes: int = Field(default=0, ge=0)
    cpu_nanoseconds: int = Field(default=0, ge=0)
    log_bytes: int = Field(default=0, ge=0)


class AgentRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    requester_user_id: uuid.UUID
    profile: ProfileName
    primary_model_id: uuid.UUID
    node_id: uuid.UUID | None = None
    node_name: str | None = None
    container_id: str | None = Field(default=None, max_length=128)
    workspace_ref: str
    state: AgentRunState
    limits: RunLimits
    exit_code: int | None = None
    failure_code: str | None = Field(default=None, max_length=64)
    failure_detail: str | None = Field(default=None, max_length=500)
    result: dict[str, Any] | None = None
    resource_usage: RunResourceUsage = Field(default_factory=RunResourceUsage)
    requested_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    killed_by: uuid.UUID | None = None
    killed_at: datetime | None = None


class AgentRunTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    from_state: AgentRunState | None
    to_state: AgentRunState
    reason: str = Field(min_length=1, max_length=500)
    occurred_at: datetime


class RunTokenStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    scope: RunTokenScope
    spent_tokens: int = Field(default=0, ge=0)
    expires_at: datetime
    revoked_at: datetime | None = None
    created_at: datetime


class RunContainerCreate(BaseModel):
    """Scheduler-authored create command; callers cannot set raw Docker controls."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    image: str = Field(pattern=r"^[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}$")
    argv: list[str] = Field(min_length=1, max_length=32)
    workspace_ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    run_token: str = Field(min_length=32, max_length=512, repr=False)
    gateway_url: str = Field(pattern=r"^https?://[^\s]+/v1/?$")
    limits: RunLimits


class RunContainerStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    container_id: str = Field(min_length=1, max_length=128)
    state: str = Field(min_length=1, max_length=32)
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    resource_usage: RunResourceUsage = Field(default_factory=RunResourceUsage)
    hardened: bool = True


class RunLogChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    offset: int = Field(ge=0)
    stream: str = Field(pattern=r"^(stdout|stderr)$")
    content: str
    truncated: bool = False


class RunCollectedResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    result: dict[str, Any]


class RunContainerObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    container_id: str
    state: str
    observed_at: datetime


class RunKillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="killed by administrator", min_length=1, max_length=500)
