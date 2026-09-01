"""Durable model-instance lifecycle and cluster-state wire contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from coire_core.models.node import Reachability, ThermalState
from coire_core.models.placement import MemoryReservation


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
    policy: str | None = Field(
        default=None,
        pattern=r"^(single:(auto|coire-[a-z0-9-]+)|pinned:coire-[a-z0-9-]+)$",
    )
    affinity_node_id: uuid.UUID | None = None


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
    budget_bytes: int = Field(ge=0)
    reserved_bytes: int = Field(ge=0)
    reservations: list[MemoryReservation] = Field(default_factory=list)


class ClusterState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: datetime
    nodes: list[ClusterNodeState]
    instances: list[ModelInstance]
