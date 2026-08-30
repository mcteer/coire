"""Node registration and status wire shapes.

`Node` is the only persisted entity in feature 000. Addresses are constrained to the
Thunderbolt mesh subnet: a node that registers an off-mesh address is a configuration error,
not something to accept quietly (spec FR-013a, ADR-0002).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from ipaddress import IPv4Address, IPv4Network

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from coire_core.models.engine import EngineStatus
from coire_core.models.jobs import JobStatus

MESH_SUBNET = IPv4Network("192.168.100.0/24")
"""The unrouted Thunderbolt mesh. See docs/adr/0002 and ARCHITECTURE.md 2.1."""


class NodeRole(StrEnum):
    STUDIO = "studio"
    CORE = "core"


class Reachability(StrEnum):
    """Feature 000 sets only HEALTHY/UNREACHABLE/UNKNOWN; DEGRADED arrives with feature 009."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


class ThermalState(StrEnum):
    NOMINAL = "nominal"
    FAIR = "fair"
    SERIOUS = "serious"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class NodePath(StrEnum):
    """Which listener answered. `FALLBACK` means the egress path was used (FR-013b/c)."""

    MESH = "mesh"
    FALLBACK = "fallback"


def _must_be_on_mesh(value: IPv4Address) -> IPv4Address:
    if value not in MESH_SUBNET:
        raise ValueError(f"address {value} is not within the mesh subnet {MESH_SUBNET}")
    return value


class NodeRegistration(BaseModel):
    """`POST /api/v1/nodes/register` request body."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^coire-[a-z0-9-]+$")
    token: SecretStr
    mesh_address: IPv4Address
    egress_address: IPv4Address
    memory_total_bytes: int = Field(gt=0)
    disk_total_bytes: int = Field(gt=0)
    gpu_cores: int | None = Field(default=None, ge=0)
    agent_version: str

    _check_mesh = field_validator("mesh_address")(_must_be_on_mesh)


class Node(BaseModel):
    """A declared Studio, as persisted and returned by the registration endpoint."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    role: NodeRole
    mesh_address: IPv4Address
    egress_address: IPv4Address
    memory_total_bytes: int
    disk_total_bytes: int
    gpu_cores: int | None = None
    agent_version: str
    registered_at: datetime
    last_seen_at: datetime
    reachability: Reachability = Reachability.UNKNOWN


class NodeStatus(BaseModel):
    """`GET /node/health` on a Studio, port 9400. Requires the node's bearer token (FR-013)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    agent_version: str
    uptime_seconds: float
    cpu_percent: float = Field(ge=0, le=100)
    gpu_percent: float | None = Field(default=None, ge=0, le=100)
    thermal_state: ThermalState = ThermalState.UNKNOWN
    memory_total_bytes: int
    memory_free_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int
    agent_cpu_percent: float
    agent_rss_bytes: int
    collection_budget_ok: bool
    path: NodePath
    sampled_at: datetime

    # --- feature 001 (additive; see specs/000-bootstrap/contracts/health-api.yaml) ---
    engines: list[EngineStatus] = Field(default_factory=list)
    """Every engine the agent owns, including orphans — with per-process CPU and resident
    memory, which is what makes FR-013 "per-process" rather than "whole node"."""
    jobs: list[JobStatus] = Field(default_factory=list)
    memory_budget_bytes: int = 0
    memory_committed_bytes: int = 0
    """Sum of the *estimates* of live engines, not their measured footprints. Admission on a
    number that moves under load is not reproducible (spec FR-020, research R6)."""
    store_free_bytes: int = 0
