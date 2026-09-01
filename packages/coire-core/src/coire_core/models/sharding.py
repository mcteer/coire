"""Wire contracts for measured links, two-rank groups and placement benchmarks."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coire_core.models.link import LinkState, RdmaState


class ShardingMode(StrEnum):
    TENSOR_PARALLEL = "tp"
    PIPELINE_PARALLEL = "pp"


class ShardCapabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[A-Za-z0-9_.-]+--[A-Za-z0-9_.-]+$")
    mode: ShardingMode


class ShardCapabilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    architecture: str = Field(min_length=1, max_length=160)
    mode: ShardingMode
    supported: bool


class ProbeTransport(StrEnum):
    JACCL = "jaccl"
    RING = "ring"


class ProbeOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ShardGroupState(StrEnum):
    PREPARING = "preparing"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class LinkObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    node_a: str
    node_b: str
    transport: ProbeTransport
    outcome: ProbeOutcome
    bandwidth_bytes_per_second: int | None = Field(default=None, gt=0)
    latency_ms: float | None = Field(default=None, ge=0)
    os_version_a: str
    os_version_b: str
    engine_version: str
    reason: str | None = Field(default=None, max_length=512)
    observed_at: datetime

    @model_validator(mode="after")
    def canonical_pair_and_measurement(self) -> LinkObservation:
        if self.node_a >= self.node_b:
            raise ValueError("link endpoints must be distinct and canonically ordered")
        if self.outcome is ProbeOutcome.SUCCEEDED and (
            self.bandwidth_bytes_per_second is None or self.latency_ms is None
        ):
            raise ValueError("successful probes require bandwidth and latency")
        return self


class StudioLinkProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_a: str
    node_b: str
    ip_state: LinkState
    rdma_state: RdmaState
    fallback_state: LinkState
    tp_eligible: bool
    required_after: datetime | None = None
    latest: list[LinkObservation] = Field(default_factory=list)
    consecutive_successes: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    flapping: bool = False
    reason: str | None = Field(default=None, max_length=512)


class LinkProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = False


class LinkProbeCommand(BaseModel):
    """Control-plane-authored probe command; inventory paths remain node configuration."""

    model_config = ConfigDict(extra="forbid")

    command_id: uuid.UUID
    transport: ProbeTransport
    hostfile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ShardRank(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=0, le=1)
    node_name: str = Field(pattern=r"^coire-edge-[ab]$")
    host: str
    port: int = Field(ge=1024, le=65535)
    pid: int | None = Field(default=None, gt=0)
    process_create_time: float | None = Field(default=None, gt=0)


class ShardGroupCommand(BaseModel):
    """Scheduler-authored command. Hosts and model slug never originate in a user request."""

    model_config = ConfigDict(extra="forbid")

    command_id: uuid.UUID
    group_id: uuid.UUID
    instance_id: uuid.UUID
    variant_id: uuid.UUID
    slug: str = Field(pattern=r"^[A-Za-z0-9_.-]+--[A-Za-z0-9_.-]+$")
    mode: ShardingMode
    ranks: list[ShardRank] = Field(min_length=2, max_length=2)
    estimate_bytes_per_rank: int = Field(gt=0)
    hostfile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exactly_two_distinct_ranks(self) -> ShardGroupCommand:
        if {rank.rank for rank in self.ranks} != {0, 1}:
            raise ValueError("a shard group requires ranks 0 and 1")
        if len({rank.node_name for rank in self.ranks}) != 2:
            raise ValueError("ranks must use distinct nodes")
        expected = {
            0: ("coire-edge-a", "coire-edge-a.fabric"),
            1: ("coire-edge-b", "coire-edge-b.fabric"),
        }
        if any((rank.node_name, rank.host) != expected[rank.rank] for rank in self.ranks):
            raise ValueError("rank topology must use the declared Studio data endpoints")
        return self


class ShardGroupStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: uuid.UUID
    instance_id: uuid.UUID
    mode: ShardingMode
    state: ShardGroupState
    ranks: list[ShardRank]
    state_reason: str | None = Field(default=None, max_length=512)
    started_at: datetime
    stopped_at: datetime | None = None


class BenchmarkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_id: uuid.UUID
    placements: list[str] = Field(
        default_factory=lambda: ["single:coire-edge-a", "sharded:tp", "sharded:pp"]
    )
    prompt_tokens: int = Field(default=128, gt=0, le=8192)
    generation_tokens: int = Field(default=128, gt=0, le=4096)

    @model_validator(mode="after")
    def complete_ordered_comparison(self) -> BenchmarkRequest:
        expected = ["single:coire-edge-a", "sharded:tp", "sharded:pp"]
        if self.placements != expected:
            raise ValueError(f"placements must be the ordered comparison {expected}")
        return self


class BenchmarkRunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BenchmarkCommand(BaseModel):
    """Registry-resolved node command; no caller path or argv is accepted."""

    model_config = ConfigDict(extra="forbid")

    command_id: uuid.UUID
    run_id: uuid.UUID
    variant_id: uuid.UUID
    slug: str = Field(pattern=r"^[A-Za-z0-9_.-]+--[A-Za-z0-9_.-]+$")
    placement: str = Field(pattern=r"^(single:coire-edge-a|sharded:(tp|pp))$")
    prompt_tokens: int = Field(gt=0, le=8192)
    generation_tokens: int = Field(gt=0, le=4096)
    hostfile_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def sharded_requires_inventory(self) -> BenchmarkCommand:
        if self.placement.startswith("sharded:") and self.hostfile_sha256 is None:
            raise ValueError("sharded benchmark requires a generated hostfile digest")
        if self.placement.startswith("single:") and self.hostfile_sha256 is not None:
            raise ValueError("single-node benchmark must not carry a hostfile")
        return self


class BenchmarkMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: uuid.UUID
    placement: str
    tokens_per_second: float | None = Field(default=None, gt=0)
    engine_version: str
    failure: str | None = Field(default=None, max_length=512)


class BenchmarkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    variant_id: uuid.UUID
    placement: str
    tokens_per_second: float | None = Field(default=None, gt=0)
    prompt_tokens: int = Field(gt=0)
    generation_tokens: int = Field(gt=0)
    gpu_cores: dict[str, int]
    os_versions: dict[str, str]
    engine_version: str
    failure: str | None = Field(default=None, max_length=512)
    run_at: datetime


class BenchmarkRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    variant_id: uuid.UUID
    state: BenchmarkRunState
    prompt_tokens: int
    generation_tokens: int
    results: list[BenchmarkResult] = Field(default_factory=list)
    failure: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
