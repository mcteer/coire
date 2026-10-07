"""Durable model-instance lifecycle and cluster-state wire contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coire_core.models.adapters import InferenceTarget
from coire_core.models.node import Reachability, ThermalState
from coire_core.models.placement import MemoryReservation
from coire_core.models.sharding import StudioLinkProjection


class InstanceState(StrEnum):
    REQUESTED = "requested"
    RESERVING = "reserving"
    LAUNCHING = "launching"
    WARMING = "warming"
    READY = "ready"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


TERMINAL_INSTANCE_STATES = frozenset({InstanceState.STOPPED, InstanceState.FAILED})


class InstanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: uuid.UUID
    variant_id: uuid.UUID
    target: InferenceTarget | None = None
    adapter_id: uuid.UUID | None = None
    policy: str | None = Field(
        default=None,
        pattern=r"^(single:(auto|coire-[a-z0-9-]+)|pinned:coire-[a-z0-9-]+|sharded:(tp|pp))$",
    )
    affinity_node_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def exact_instance_target(self) -> InstanceCreate:
        if self.target is not None:
            if self.target.model_id != self.model_id or self.target.variant_id != self.variant_id:
                raise ValueError("instance target must match its parent and variant")
            if self.adapter_id is not None and self.adapter_id != self.target.adapter_id:
                raise ValueError("instance adapter differs from exact target")
        if (
            (
                self.adapter_id is not None
                or (self.target is not None and self.target.adapter_id is not None)
            )
            and self.policy
            and self.policy.startswith("sharded:")
        ):
            raise ValueError("adapter instances are single-node only")
        return self


class InstanceMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: uuid.UUID
    node_name: str
    rank: int = Field(ge=0)
    engine_id: uuid.UUID | None = None
    reservation_id: uuid.UUID | None = None
    host: str
    port: int | None = Field(default=None, ge=1, le=65535)


class InstanceTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    instance_id: uuid.UUID
    sequence: int = Field(ge=1)
    previous_state: InstanceState | None = None
    state: InstanceState
    reason: str | None = None
    at: datetime


class ModelInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    model_id: uuid.UUID
    variant_id: uuid.UUID
    target: InferenceTarget | None = None
    placement_decision_id: uuid.UUID | None = None
    policy: str
    state: InstanceState
    effective_state: InstanceState
    failure_code: str | None = None
    failure_detail: str | None = None
    in_flight: int = Field(ge=0)
    members: list[InstanceMember] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    transitioned_at: datetime
    drain_deadline: datetime | None = None
    fallback_attempted_at: datetime | None = None
    fallback_instance_id: uuid.UUID | None = None
    fallback_no_fit: bool = False


class NodeDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^coire-[a-z0-9-]+$")
    control_host: str = Field(pattern=r"^coire-[a-z0-9-]+(?:\.lab)?$")
    data_host: str | None = Field(default=None, pattern=r"^coire-edge-[ab]\.fabric$")
    memory_total_bytes: int = Field(gt=0)
    disk_total_bytes: int = Field(gt=0)
    gpu_cores: int | None = Field(default=None, ge=0)


class NodeRegistrationCredential(BaseModel):
    """One-time plaintext response. The database stores only its digest."""

    model_config = ConfigDict(extra="forbid")

    node_id: uuid.UUID
    token: str = Field(min_length=32)
    issued_at: datetime


class ClusterNodeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    reachability: Reachability
    health_observed_at: datetime | None = None
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    gpu_percent: float | None = Field(default=None, ge=0, le=100)
    thermal_state: ThermalState = ThermalState.UNKNOWN
    health_reason: str | None = None
    stale: bool = False
    memory_total_bytes: int | None = Field(default=None, ge=0)
    memory_free_bytes: int | None = Field(default=None, ge=0)
    disk_total_bytes: int | None = Field(default=None, ge=0)
    disk_free_bytes: int | None = Field(default=None, ge=0)
    budget_bytes: int = Field(ge=0)
    reserved_bytes: int = Field(ge=0)
    reservations: list[MemoryReservation] = Field(default_factory=list)


class ClusterState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: datetime
    nodes: list[ClusterNodeState]
    instances: list[ModelInstance]
    studio_link: StudioLinkProjection | None = None
