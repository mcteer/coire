"""Health and readiness wire shapes.

These are the only declarations of these shapes in the platform (spec FR-002). The generated
OpenAPI document is checked against `specs/000-bootstrap/contracts/health-api.yaml`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coire_core.models.link import StudioDataLinkStatus
from coire_core.models.node import NodeStatus, Reachability


class HealthStatus(StrEnum):
    """Aggregate verdict for the control plane."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ServiceHealth(BaseModel):
    """One dependency's health as observed by the aggregate probe."""

    model_config = ConfigDict(extra="forbid")

    name: str
    healthy: bool
    detail: str | None = None
    checked_at: datetime
    latency_ms: float | None = None


class HealthResponse(BaseModel):
    """`GET /health` — the aggregate. HTTP 503 when `status` is `unhealthy`."""

    model_config = ConfigDict(extra="forbid")

    status: HealthStatus
    version: str
    services: list[ServiceHealth] = Field(default_factory=list)
    nodes: list[NodeHealth] = Field(default_factory=list)
    links: list[StudioDataLinkStatus] = Field(default_factory=list)
    generated_at: datetime


class ReadyResponse(BaseModel):
    """`GET /ready` — liveness only. Deliberately checks nothing external (FR-009)."""

    model_config = ConfigDict(extra="forbid")

    service: str
    version: str
    ready: Literal[True] = True


class ProcessObservation(BaseModel):
    """Bounded process resource observation; never an engine-control input."""

    model_config = ConfigDict(extra="forbid")

    pid: int = Field(gt=0)
    kind: str = Field(max_length=32)
    model_id: str | None = Field(default=None, max_length=255)
    cpu_percent: float = Field(ge=0)
    rss_bytes: int = Field(ge=0)
    verified_at: datetime


class NodeHealth(BaseModel):
    """A node verdict evaluated using control-plane receipt time."""

    model_config = ConfigDict(extra="forbid")

    name: str
    verdict: Reachability
    reason: str | None = Field(default=None, max_length=255)
    fresh: bool
    last_observed_at: datetime
    last_success_at: datetime | None = None
    seconds_since_heartbeat: float = Field(ge=0)
    heartbeat_latency_ms: float | None = Field(default=None, ge=0)
    clock_skew_seconds: float | None = None
    process_state_verified: bool
    observation: NodeStatus | None = None
