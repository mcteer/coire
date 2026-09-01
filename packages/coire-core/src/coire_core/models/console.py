"""Typed projections consumed by the administrative console."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coire_core.models.instance import ClusterState
from coire_core.models.placement import MemoryLedger


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
