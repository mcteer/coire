"""Typed projections consumed by the administrative console."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coire_core.models.evaluation_links import EvaluationGroupLink
from coire_core.models.files import ULID_PATTERN
from coire_core.models.instance import ClusterState
from coire_core.models.placement import MemoryLedger
from coire_core.models.preference import PreferenceProbe
from coire_core.models.training import TrainingJobState


class CursorPage[T](BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[T]
    next_cursor: str | None = None


class ConsoleCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster: bool = True
    models: bool = True
    instances: bool = True
    jobs: bool = True
    identity: bool = True
    audit: bool = True
    ask: bool = True
    training: bool = False


class ConsoleAlert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: str = Field(pattern=r"^(info|warning|critical)$")
    title: str = Field(min_length=1, max_length=160)
    detail: str = Field(min_length=1, max_length=500)
    target_id: str | None = None


class CoreHostCapacity(BaseModel):
    """Capacity visible to the core control-plane runtime, not fabricated host telemetry."""

    model_config = ConfigDict(extra="forbid")

    host_name: str = Field(min_length=1, max_length=128)
    health: Literal["healthy", "degraded", "unreachable"]
    memory_total_bytes: int = Field(ge=0)
    memory_free_bytes: int = Field(ge=0)
    disk_total_bytes: int = Field(ge=0)
    disk_free_bytes: int = Field(ge=0)
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    observed_at: datetime
    source: Literal["core-control-plane-runtime"] = "core-control-plane-runtime"


class ConsoleSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: datetime
    cursor: str
    capabilities: ConsoleCapabilities
    cluster: ClusterState
    core: CoreHostCapacity | None = None
    ledgers: list[MemoryLedger]
    alerts: list[ConsoleAlert] = Field(default_factory=list)


class ConsoleEventKind(StrEnum):
    SNAPSHOT = "snapshot"
    RECONCILE = "reconcile"


class ConsoleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: ConsoleEventKind
    observed_at: datetime
    snapshot: ConsoleSnapshot


class ActivityKind(StrEnum):
    JOB = "job"
    INSTANCE = "instance"
    IMAGE_WORKER = "image_worker"


class ActivityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    kind: ActivityKind
    owner: str
    target: str
    state: str
    started_at: datetime
    elapsed_seconds: float = Field(ge=0)
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    failure_reason: str | None = None
    can_stop: bool


class ImageActivityItem(BaseModel):
    """Separate ULID image-job projection; no prompt or storage path is exposed."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(pattern=ULID_PATTERN)
    owner_id: uuid.UUID
    model_id: uuid.UUID
    state: Literal[
        "queued",
        "reserving",
        "running",
        "transferring",
        "cancelling",
        "cancelled",
        "failed",
        "succeeded",
    ]
    started_at: datetime
    elapsed_seconds: float = Field(default=0, ge=0)
    progress_step: int | None = Field(default=None, ge=0)
    progress_total: int | None = Field(default=None, ge=1)
    safe_failure_code: str | None = Field(default=None, max_length=100)
    can_stop: bool


class TrainingActivityItem(BaseModel):
    objective: Literal["dpo", "orpo"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    preference_probe: PreferenceProbe | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    evaluation_groups: list[EvaluationGroupLink] = Field(
        default_factory=list, max_length=100, exclude_if=lambda value: not value
    )
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    job_id: str = Field(pattern=ULID_PATTERN)
    owner_id: uuid.UUID
    model_id: uuid.UUID
    variant_id: uuid.UUID
    state: TrainingJobState
    completed_update: int = Field(ge=0)
    total_updates: int = Field(ge=1)
    reserved_bytes: int = Field(ge=0)
    can_stop: bool
    safe_reason: str | None = Field(default=None, max_length=64)
    version: int = Field(ge=1)
    adapter_slug: str = Field(min_length=1, max_length=63)
    started_at: datetime
    latest_train_loss: float | None = None
    latest_validation_loss: float | None = None


class AskStatus(StrEnum):
    ANSWERED = "answered"
    UNAVAILABLE = "unavailable"


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000)


class AskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AskStatus
    answer: str
    observed_at: datetime
    sources: list[str] = Field(default_factory=list)
