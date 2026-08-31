"""Typed observations for the control paths and Studio-only data link."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coire_core.models.node import Reachability


class LinkState(StrEnum):
    UNKNOWN = "unknown"
    UP = "up"
    DOWN = "down"


class RdmaState(StrEnum):
    UNKNOWN = "unknown"
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


class ControlPathStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_name: str = Field(pattern=r"^coire-edge-[ab]$")
    state: Reachability = Reachability.UNKNOWN
    latency_ms: float | None = Field(default=None, ge=0)
    consecutive_successes: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=512)


class StudioDataLinkStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_a: str = Field(pattern=r"^coire-edge-[ab]$")
    node_b: str = Field(pattern=r"^coire-edge-[ab]$")
    ip_state: LinkState = LinkState.UNKNOWN
    rdma_state: RdmaState = RdmaState.UNKNOWN
    bandwidth_bytes_per_second: int | None = Field(default=None, gt=0)
    latency_ms: float | None = Field(default=None, ge=0)
    measured_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def canonical_pair(self) -> StudioDataLinkStatus:
        if self.node_a == self.node_b:
            raise ValueError("Studio data-link endpoints must be distinct")
        if self.node_a > self.node_b:
            raise ValueError("Studio data-link endpoints must use canonical order")
        return self
