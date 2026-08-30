"""Health and readiness wire shapes.

These are the only declarations of these shapes in the platform (spec FR-002). The generated
OpenAPI document is checked against `specs/000-bootstrap/contracts/health-api.yaml`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    nodes: list[ServiceHealth] = Field(default_factory=list)
    generated_at: datetime


class ReadyResponse(BaseModel):
    """`GET /ready` — liveness only. Deliberately checks nothing external (FR-009)."""

    model_config = ConfigDict(extra="forbid")

    service: str
    version: str
    ready: Literal[True] = True
