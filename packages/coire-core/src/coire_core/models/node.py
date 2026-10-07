"""Node registration and status wire shapes.

`Node` is the only persisted entity in feature 000. Addresses are constrained to the
Thunderbolt mesh subnet: a node that registers an off-mesh address is a configuration error,
not something to accept quietly (spec FR-013a, ADR-0002).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from datetime import datetime
from enum import StrEnum
from ipaddress import IPv4Address, IPv4Network
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from coire_core.models.engine import EngineStatus
from coire_core.models.harness import HarnessRunRequest, TaskClass
from coire_core.models.jobs import JobStatus
from coire_core.models.mcp import WorkspaceSource
from coire_core.models.registry import EngineBackend
from coire_core.models.training_node import NodeTrainingStatus

MESH_SUBNET = IPv4Network("192.168.100.0/24")
"""The unrouted Thunderbolt mesh. See docs/adr/0002 and ARCHITECTURE.md 2.1."""


class WorkspaceVisualInput(BaseModel):
    """API-verified PNG bytes staged under a generated asset ID on one Studio."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    asset_id: uuid.UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int = Field(ge=1, le=2048)
    height: int = Field(ge=1, le=2048)
    data_base64: str = Field(min_length=1, max_length=13_981_016, repr=False)

    @model_validator(mode="after")
    def verified_png(self) -> WorkspaceVisualInput:
        try:
            data = base64.b64decode(self.data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("visual input encoding is invalid") from exc
        if (
            len(data) > 10 * 1024 * 1024
            or len(data) < 24
            or data[:8] != b"\x89PNG\r\n\x1a\n"
            or data[12:16] != b"IHDR"
            or int.from_bytes(data[16:20], "big") != self.width
            or int.from_bytes(data[20:24], "big") != self.height
            or hashlib.sha256(data).hexdigest() != self.sha256
        ):
            raise ValueError("visual input manifest does not match PNG bytes")
        return self


class WorkspacePrepareRequest(BaseModel):
    """Scheduler-authored, bounded Studio workspace preparation command."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    run_id: uuid.UUID
    source: WorkspaceSource
    task_class: TaskClass
    harness_request: HarnessRunRequest
    visual_inputs: list[WorkspaceVisualInput] = Field(default_factory=list, max_length=10)
    max_bytes: int = Field(default=512 * 1024 * 1024, ge=1024, le=8 * 1024**3)
    timeout_seconds: int = Field(default=120, ge=1, le=900)

    @model_validator(mode="after")
    def task_class_matches(self) -> WorkspacePrepareRequest:
        if self.task_class is not self.harness_request.task_class:
            raise ValueError("task class does not match harness request")
        referenced = [
            image for message in self.harness_request.history for image in message.visual_inputs
        ] + self.harness_request.visual_inputs
        references = {image.asset_id: image for image in referenced}
        staged = {image.asset_id: image for image in self.visual_inputs}
        if len(staged) != len(self.visual_inputs) or set(references) != set(staged):
            raise ValueError("staged visual inputs do not match harness references")
        if sum(len(image.data_base64) for image in self.visual_inputs) > 44_739_240:
            raise ValueError("visual control inputs exceed the run bound")
        for image in referenced:
            supplied = staged[image.asset_id]
            if (
                image.media_type != "image/png"
                or image.width != supplied.width
                or image.height != supplied.height
            ):
                raise ValueError("visual control input metadata changed")
        return self


class WorkspacePrepareResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    workspace_ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    output_ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    prepared_at: datetime


class WorkspaceCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    preserve_for_recovery: bool = False


class WorkspaceArtifactStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    artifact_id: uuid.UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    collected_at: datetime


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


class NetworkPath(StrEnum):
    """A request's fixed purpose; unlike ``NodePath`` this never implies fallback."""

    CONTROL = "control"
    DATA = "data"


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
    egress_address: IPv4Address | None = None
    """Optional: a node may have no route off the mesh at all, which is a legitimate and
    rather hardened configuration. It is used only for the alerted Wi-Fi fallback listener
    (feature 000 FR-013a), so its absence costs that fallback and nothing else."""
    memory_total_bytes: int = Field(gt=0)
    disk_total_bytes: int = Field(gt=0)
    gpu_cores: int | None = Field(default=None, ge=0)
    agent_version: str

    _check_mesh = field_validator("mesh_address")(_must_be_on_mesh)


NodeRegistrationV1 = NodeRegistration


class NodeEndpointSet(BaseModel):
    """Stable endpoint identities advertised by a v2 node agent."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[2] = 2
    control_host: str = Field(pattern=r"^coire-[a-z0-9-]+(?:\.lab)?$")
    data_host: str | None = Field(default=None, pattern=r"^coire-edge-[ab]\.fabric$")


class NodeRegistrationV2(BaseModel):
    """Separated-fabric registration shape (feature 022)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^coire-[a-z0-9-]+$")
    token: SecretStr
    endpoints: NodeEndpointSet
    memory_total_bytes: int = Field(gt=0)
    disk_total_bytes: int = Field(gt=0)
    gpu_cores: int | None = Field(default=None, ge=0)
    agent_version: str

    @model_validator(mode="after")
    def validate_endpoint_identity(self) -> NodeRegistrationV2:
        if self.endpoints.control_host not in {self.name, f"{self.name}.lab"}:
            raise ValueError("control_host must match the registering node name or its .lab FQDN")
        if self.name in {"coire-edge-a", "coire-edge-b"} and self.endpoints.data_host is None:
            raise ValueError("declared Studio nodes require a data_host")
        if self.name == "coire-core" and self.endpoints.data_host is not None:
            raise ValueError("core must not advertise a data_host")
        return self


class Node(BaseModel):
    """A declared Studio, as persisted and returned by the registration endpoint."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    role: NodeRole
    mesh_address: IPv4Address
    egress_address: IPv4Address | None = None
    memory_total_bytes: int
    disk_total_bytes: int
    gpu_cores: int | None = None
    agent_version: str
    registered_at: datetime
    last_seen_at: datetime
    reachability: Reachability = Reachability.UNKNOWN


NodeV1 = Node


class NodeV2(BaseModel):
    """Persisted node response matching a v2 registration request."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    role: NodeRole
    endpoints: NodeEndpointSet
    memory_total_bytes: int = Field(gt=0)
    disk_total_bytes: int = Field(gt=0)
    gpu_cores: int | None = Field(default=None, ge=0)
    agent_version: str
    registered_at: datetime
    last_seen_at: datetime
    reachability: Reachability = Reachability.UNKNOWN


class NodeStatus(BaseModel):
    """`GET /node/health` on a Studio, port 9400. Requires the node's bearer token (FR-013)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    agent_version: str
    os_version: str = "unknown"
    engine_version: str = "unknown"
    uptime_seconds: float
    cpu_percent: float = Field(ge=0, le=100)
    gpu_percent: float | None = Field(default=None, ge=0, le=100)
    thermal_state: ThermalState = ThermalState.UNKNOWN
    memory_total_bytes: int
    memory_free_bytes: int
    swap_used_bytes: int | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)
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
    training: list[NodeTrainingStatus] = Field(
        default_factory=list, max_length=8, exclude_if=lambda value: not value
    )
    memory_budget_bytes: int = 0
    memory_committed_bytes: int = 0
    """Disjoint engine, image-worker and acquisition holds; not measured footprint."""
    image_worker_resident_bytes: int | None = Field(default=None, ge=0)
    store_free_bytes: int = 0
    supported_backends: list[EngineBackend] = Field(default_factory=lambda: [EngineBackend.MLX_LM])


class NodeStatusV2(BaseModel):
    """Control-listener health response for separated-fabric agents."""

    model_config = ConfigDict(extra="forbid")

    name: str
    agent_version: str
    os_version: str = "unknown"
    engine_version: str = "unknown"
    uptime_seconds: float = Field(ge=0)
    cpu_percent: float = Field(ge=0, le=100)
    gpu_percent: float | None = Field(default=None, ge=0, le=100)
    thermal_state: ThermalState = ThermalState.UNKNOWN
    memory_total_bytes: int = Field(gt=0)
    memory_free_bytes: int = Field(ge=0)
    swap_used_bytes: int | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)
    disk_total_bytes: int = Field(gt=0)
    disk_free_bytes: int = Field(ge=0)
    agent_cpu_percent: float = Field(ge=0)
    agent_rss_bytes: int = Field(ge=0)
    collection_budget_ok: bool
    path: Literal[NetworkPath.CONTROL] = NetworkPath.CONTROL
    sampled_at: datetime
    engines: list[EngineStatus] = Field(default_factory=list)
    jobs: list[JobStatus] = Field(default_factory=list)
    training: list[NodeTrainingStatus] = Field(
        default_factory=list, max_length=8, exclude_if=lambda value: not value
    )
    memory_budget_bytes: int = Field(default=0, ge=0)
    memory_committed_bytes: int = Field(default=0, ge=0)
    image_worker_resident_bytes: int | None = Field(default=None, ge=0)
    store_free_bytes: int = Field(default=0, ge=0)
    supported_backends: list[EngineBackend] = Field(default_factory=lambda: [EngineBackend.MLX_LM])
    run_images_configured: bool = False
