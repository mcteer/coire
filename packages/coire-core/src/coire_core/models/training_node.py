"""Fenced Studio training commands and inert immutable artifact manifests."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from coire_core.models.datasets import DatasetAnalysis, DatasetAnalysisBinding, SplitManifest
from coire_core.models.training import (
    ResolvedTrainingSpec,
    TrainingMetricSample,
    TrainingOptimizer,
    TrainingReason,
    TrainingResidentTarget,
)
from coire_core.models.training_types import Digest, StudioName, TrainingId, TrainingWire

type MeasurementThermalState = Literal["nominal", "fair", "serious", "critical", "unknown"]


class TrainingCommand(TrainingWire):
    schema_version: Literal[1] = 1
    command_id: uuid.UUID
    job_id: TrainingId
    attempt_id: TrainingId
    fence: int = Field(strict=True, ge=1)
    request_sha256: Digest
    node: StudioName
    rank: int = Field(strict=True, ge=0, le=1)
    world_size: Literal[1, 2]
    lease_expires_at: AwareDatetime

    @model_validator(mode="after")
    def valid_rank(self) -> TrainingCommand:
        if self.rank >= self.world_size:
            raise ValueError("rank must be below world size")
        if self.world_size == 2 and self.node != (
            "coire-edge-a" if self.rank == 0 else "coire-edge-b"
        ):
            raise ValueError("two-rank mapping must match declared Studio order")
        return self


class TrainingArtifactFile(TrainingWire):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_-]+\.(safetensors|json)$")
    bytes: int = Field(strict=True, ge=1, le=20 * 1024**3)
    sha256: Digest


class TensorDescriptor(TrainingWire):
    key: str = Field(min_length=1, max_length=512, pattern=r"^[a-zA-Z0-9_.-]+$")
    shape: list[int] = Field(max_length=8)
    dtype: Literal["float32", "float16", "bfloat16", "uint32", "int32", "int64", "uint64", "bool"]

    @model_validator(mode="after")
    def bounded_shape(self) -> TensorDescriptor:
        if any(dimension < 0 or dimension > 2**31 - 1 for dimension in self.shape):
            raise ValueError("tensor dimensions must be bounded nonnegative integers")
        return self


class RankStateManifest(TrainingWire):
    rank: int = Field(strict=True, ge=0, le=1)
    update: int = Field(strict=True, ge=0, le=100_000)
    adapter_file_id: str = Field(min_length=1, max_length=128)
    optimizer_file_id: str = Field(min_length=1, max_length=128)
    state_file_id: str = Field(min_length=1, max_length=128)
    adapter_tensors: list[TensorDescriptor] = Field(min_length=1, max_length=100_000)
    optimizer_tensors: list[TensorDescriptor] = Field(min_length=1, max_length=200_000)


class TrainingArtifactManifest(TrainingWire):
    schema_version: Literal[1] = 1
    artifact_id: uuid.UUID
    kind: Literal["checkpoint", "adapter"]
    files: list[TrainingArtifactFile] = Field(min_length=1, max_length=256)
    total_bytes: int = Field(strict=True, ge=1, le=20 * 1024**3)
    job_id: TrainingId | None = None
    attempt_id: TrainingId | None = None
    fence: int | None = Field(default=None, strict=True, ge=1)
    update: int | None = Field(default=None, strict=True, ge=0, le=100_000)
    world_size: Literal[1, 2] | None = None
    runtime_sha256: Digest | None = None
    resolved_spec_sha256: Digest | None = None
    ranks: list[RankStateManifest] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def complete_manifest(self) -> TrainingArtifactManifest:
        if len({item.id for item in self.files}) != len(self.files) or len(
            {item.name for item in self.files}
        ) != len(self.files):
            raise ValueError("artifact file IDs and names must be unique")
        if sum(item.bytes for item in self.files) != self.total_bytes:
            raise ValueError("artifact total differs from file bytes")
        if self.kind == "checkpoint":
            if any(
                value is None
                for value in (
                    self.job_id,
                    self.attempt_id,
                    self.fence,
                    self.update,
                    self.world_size,
                    self.runtime_sha256,
                    self.resolved_spec_sha256,
                )
            ):
                raise ValueError("checkpoint requires full attempt and runtime identity")
            if {rank.rank for rank in self.ranks} != set(range(self.world_size or 0)) or len(
                self.ranks
            ) != self.world_size:
                raise ValueError("checkpoint requires each rank exactly once")
            file_ids = {file.id for file in self.files}
            for rank in self.ranks:
                if (
                    rank.update != self.update
                    or not {rank.adapter_file_id, rank.optimizer_file_id, rank.state_file_id}
                    <= file_ids
                ):
                    raise ValueError("rank state must match common update and manifest files")
        elif self.ranks:
            raise ValueError("serving adapter artifact cannot contain optimizer rank state")
        return self

    def canonical_sha256(self) -> str:
        data = self.model_dump(mode="json")
        data["files"] = sorted(data["files"], key=lambda item: item["id"])
        data["ranks"] = sorted(data["ranks"], key=lambda item: item["rank"])
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()


class TrainingCollectiveBinding(TrainingWire):
    backend: Literal["jaccl"] = "jaccl"
    hostfile_sha256: Digest
    coordinator_port: int = Field(strict=True, ge=1024, le=65535)
    runtime_sha256: Digest
    world_size: Literal[2] = 2


class TrainingRankComponentManifest(TrainingWire):
    artifact_id: uuid.UUID
    job_id: TrainingId
    attempt_id: TrainingId
    fence: int = Field(strict=True, ge=1)
    update: int = Field(strict=True, ge=0, le=100_000)
    rank: int = Field(strict=True, ge=0, le=1)
    world_size: Literal[2] = 2
    runtime_sha256: Digest
    resolved_spec_sha256: Digest
    state: RankStateManifest
    files: list[TrainingArtifactFile] = Field(min_length=3, max_length=3)
    total_bytes: int = Field(strict=True, ge=1, le=20 * 1024**3)

    @model_validator(mode="after")
    def complete_rank(self) -> TrainingRankComponentManifest:
        if (
            self.state.rank != self.rank
            or self.state.update != self.update
            or sum(f.bytes for f in self.files) != self.total_bytes
            or len({f.id for f in self.files}) != 3
            or len({f.name for f in self.files}) != 3
            or {self.state.adapter_file_id, self.state.optimizer_file_id, self.state.state_file_id}
            != {f.id for f in self.files}
        ):
            raise ValueError("rank component must contain exactly its evaluated full-state files")
        return self

    def canonical_sha256(self) -> str:
        data = self.model_dump(mode="json")
        data["files"] = sorted(data["files"], key=lambda file: file["id"])
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()


class TrainingPrepareRequest(TrainingCommand):
    resolved: ResolvedTrainingSpec
    reservation_id: uuid.UUID
    disk_reservation_id: uuid.UUID
    resume_manifest_sha256: Digest | None = None
    resume_checkpoint_id: uuid.UUID | None = None
    collective: TrainingCollectiveBinding | None = None

    @model_validator(mode="after")
    def compatible_resume(self) -> TrainingPrepareRequest:
        if (self.resume_checkpoint_id is None) != (self.resume_manifest_sha256 is None):
            raise ValueError("resume needs both checkpoint identity and manifest")
        expected = 2 if self.resolved.spec.placement.mode == "data_parallel" else 1
        if expected != self.world_size:
            raise ValueError("node world size differs from resolved training placement")
        if self.collective is not None and (
            self.world_size != 2 or self.collective.runtime_sha256 != self.resolved.runtime_sha256
        ):
            raise ValueError("collective binding must match the frozen two-rank runtime")
        return self


class TrainingPrepared(TrainingWire):
    attempt_id: TrainingId
    fence: int = Field(ge=1)
    node: StudioName
    reservation_id: uuid.UUID
    runtime_sha256: Digest
    ready: bool
    reason: TrainingReason | None = None


class TrainingMeasurementPrepare(TrainingWire):
    """A probe ceiling is execution authority, never resource evidence."""

    measurement_id: uuid.UUID
    prepare: TrainingPrepareRequest
    hardware_sha256: Digest
    deadline: AwareDatetime
    mode: Literal["memory", "coexistence"]
    resident_targets: list[TrainingResidentTarget] = Field(default_factory=list, max_length=32)
    resident_engine_ids: dict[uuid.UUID, uuid.UUID] = Field(default_factory=dict, max_length=32)
    """Logical instance to owned engine identity, frozen by authenticated admission."""

    @model_validator(mode="after")
    def bounded_probe(self) -> TrainingMeasurementPrepare:
        if self.prepare.resume_checkpoint_id is not None:
            raise ValueError("measurement must start from the pinned base")
        if self.deadline <= self.prepare.lease_expires_at:
            raise ValueError("measurement deadline must follow initial lease")
        return self


class TrainingMeasurementCapabilities(TrainingWire):
    node: StudioName
    hardware_sha256: Digest
    world_sizes: list[Literal[1, 2]] = Field(max_length=2)
    measurement_checkpoint: bool

    @model_validator(mode="after")
    def actual_matrix(self) -> TrainingMeasurementCapabilities:
        if len(set(self.world_sizes)) != len(self.world_sizes) or (
            2 in self.world_sizes and not self.measurement_checkpoint
        ):
            raise ValueError(
                "world-size advertisement must have unique sizes and a real rank measurement hook"
            )
        return self


class TrainingMeasurementRankCheckpoint(TrainingWire):
    """Observed local serialized rank state; not a common artifact/durability claim."""

    measurement_id: uuid.UUID
    job_id: TrainingId
    attempt_id: TrainingId
    fence: int = Field(strict=True, ge=1)
    rank: Literal[0, 1]
    node: StudioName
    update: int = Field(strict=True, ge=1, le=100_000)
    runtime_sha256: Digest
    resolved_spec_sha256: Digest
    component_sha256: Digest
    serialized_bytes: int = Field(strict=True, ge=1, le=20 * 1024**3)
    optimizer_bytes: int = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def declared_rank(self) -> TrainingMeasurementRankCheckpoint:
        if self.node != ("coire-edge-a" if self.rank == 0 else "coire-edge-b"):
            raise ValueError("measured rank must match declared Studio mapping")
        return self


class TrainingMeasurementRankSet(TrainingWire):
    ranks: list[TrainingMeasurementRankCheckpoint] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def common_boundary(self) -> TrainingMeasurementRankSet:
        if {r.rank for r in self.ranks} != {0, 1}:
            raise ValueError("measurement requires both actual rank summaries")
        if (
            len(
                {
                    (
                        r.measurement_id,
                        r.job_id,
                        r.attempt_id,
                        r.fence,
                        r.update,
                        r.runtime_sha256,
                        r.resolved_spec_sha256,
                    )
                    for r in self.ranks
                }
            )
            != 1
        ):
            raise ValueError("rank summaries must share the evaluated immutable boundary")
        return self

    @property
    def serialized_bytes(self) -> int:
        return sum(r.serialized_bytes for r in self.ranks)

    def canonical_sha256(self) -> str:
        value = self.model_dump(mode="json")
        value["ranks"] = sorted(value["ranks"], key=lambda r: r["rank"])
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()


class TrainingMeasurementObservation(TrainingWire):
    measurement_id: uuid.UUID
    attempt_id: TrainingId
    node: StudioName
    hardware_sha256: Digest
    request_sha256: Digest
    completed_updates: int = Field(strict=True, ge=1, le=100_000)
    elapsed_seconds: float = Field(gt=0, allow_inf_nan=False)
    training_started_at: AwareDatetime
    training_finished_at: AwareDatetime
    peak_footprint_bytes: int = Field(strict=True, ge=1)
    peak_mlx_bytes: int = Field(strict=True, ge=1)
    weight_bytes: int = Field(strict=True, ge=1)
    adapter_bytes: int = Field(strict=True, ge=1)
    optimizer_bytes: int = Field(strict=True, ge=1)
    buffer_bytes: int = Field(strict=True, ge=0)
    checkpoint_bytes: int = Field(strict=True, ge=1)
    serialization_peak_bytes: int = Field(strict=True, ge=1)
    swap_growth_bytes: int = Field(strict=True, ge=0)
    thermal_ok: bool
    sample_count: int = Field(strict=True, ge=1)
    measured_at: AwareDatetime
    rank: Literal[0, 1] = 0
    world_size: Literal[1, 2] = 1
    rank_checkpoints: list[TrainingMeasurementRankCheckpoint] = Field(
        default_factory=list, max_length=2
    )

    @model_validator(mode="after")
    def real_rank_serialization(self) -> TrainingMeasurementObservation:
        if self.rank >= self.world_size:
            raise ValueError("measurement rank must be below world size")
        if self.world_size == 2:
            pair = TrainingMeasurementRankSet(ranks=self.rank_checkpoints)
            local = next(r for r in pair.ranks if r.rank == self.rank)
            if (
                local.measurement_id != self.measurement_id
                or local.attempt_id != self.attempt_id
                or local.node != self.node
                or local.update > self.completed_updates
                or pair.serialized_bytes != self.checkpoint_bytes
            ):
                raise ValueError(
                    "observation must bind both measured rank sizes and its local identity"
                )
        elif self.rank_checkpoints:
            raise ValueError("single rank cannot claim peer checkpoint measurements")
        return self


class TrainingMeasurementSource(TrainingWire):
    binding: DatasetAnalysisBinding
    split: SplitManifest
    analysis: DatasetAnalysis


class TrainingMeasurementBinding(TrainingWire):
    base_manifest_sha256: Digest
    tokenizer_sha256: Digest
    template_sha256: Digest
    runtime_sha256: Digest
    worker_version: str = Field(min_length=1, max_length=64)
    sources: list[TrainingMeasurementSource] = Field(min_length=1, max_length=32)


class TrainingMeasurementDispatch(TrainingWire):
    commands: list[TrainingMeasurementPrepare] = Field(min_length=1, max_length=2)
    sources: list[TrainingMeasurementSource] = Field(min_length=1, max_length=32)
    spawn_nonce: uuid.UUID

    @model_validator(mode="after")
    def exact_participants(self) -> TrainingMeasurementDispatch:
        if len({c.prepare.node for c in self.commands}) != len(self.commands):
            raise ValueError("measurement participants must be unique")
        if len({c.measurement_id for c in self.commands}) != 1:
            raise ValueError("measurement participants must share identity")
        prepares = [c.prepare for c in self.commands]
        if (
            {p.rank for p in prepares} != set(range(len(prepares)))
            or any(p.world_size != len(prepares) for p in prepares)
            or len({(p.job_id, p.attempt_id, p.fence) for p in prepares}) != 1
            or any(p.resolved != prepares[0].resolved for p in prepares)
        ):
            raise ValueError(
                "measurement requires one shared attempt and full resolved envelope on every rank"
            )
        return self


class TrainingStartRequest(TrainingCommand):
    prepared_command_id: uuid.UUID
    spawn_nonce: uuid.UUID


class TrainingStartReceipt(TrainingWire):
    attempt_id: TrainingId
    fence: int = Field(ge=1)
    pid: int = Field(ge=1)
    process_create_time: float = Field(gt=0)
    reservation_id: uuid.UUID


class TrainingStopRequest(TrainingCommand):
    reason: TrainingReason


class TrainingPauseRequest(TrainingCommand):
    reason: Literal[
        "admin_pause", "latency_breach", "thermal_breach", "memory_breach", "lease_expired"
    ]


class TrainingLeaseRenewal(TrainingCommand):
    pass


class TrainingStopReceipt(TrainingWire):
    attempt_id: TrainingId
    fence: int = Field(ge=1)
    node: StudioName
    pid: int | None = Field(default=None, ge=1)
    process_create_time: float | None = Field(default=None, gt=0)
    stopped: bool
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def matching_process_identity(self) -> TrainingStopReceipt:
        if (self.pid is None) != (self.process_create_time is None):
            raise ValueError("process stop identity requires pid and create time together")
        return self


class NodeTrainingStatus(TrainingWire):
    attempt_id: TrainingId
    job_id: TrainingId
    fence: int = Field(ge=1)
    node: StudioName
    liveness: Literal["prepared", "running", "stopping", "stopped", "unknown", "orphan"]
    pid: int | None = Field(default=None, ge=1)
    process_create_time: float | None = Field(default=None, gt=0)
    update: int = Field(ge=0, le=100_000)
    footprint_bytes: int | None = Field(default=None, ge=0)
    lease_expires_at: AwareDatetime
    latest_manifest_sha256: Digest | None = None
    reason: TrainingReason | None = None


class TrainingMeasurementNodeStatus(TrainingWire):
    measurement_id: uuid.UUID
    status: NodeTrainingStatus
    observation: TrainingMeasurementObservation | None = None
    stopped: bool
    ready: bool = False
    training_started_at: AwareDatetime | None = None
    swap_used_bytes: int | None = Field(default=None, ge=0)
    swap_out_bytes: int | None = Field(default=None, ge=0)
    thermal_state: MeasurementThermalState = "unknown"
    sampled_at: AwareDatetime | None = None


class CheckpointCommitAcknowledgement(TrainingCommand):
    checkpoint_id: uuid.UUID
    manifest_sha256: Digest
    update: int = Field(strict=True, ge=0, le=100_000)


class TrainingArtifactGrantRequest(TrainingWire):
    command_id: uuid.UUID
    artifact_id: uuid.UUID
    manifest_sha256: Digest
    source_node: StudioName
    destination_node: StudioName
    attempt_id: TrainingId
    fence: int = Field(strict=True, ge=1)
    file_ids: list[str] = Field(min_length=1, max_length=256)
    max_bytes: int = Field(strict=True, ge=1, le=20 * 1024**3)
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def peer_and_files(self) -> TrainingArtifactGrantRequest:
        if self.source_node == self.destination_node:
            raise ValueError("artifact transfer requires different declared peers")
        if len(set(self.file_ids)) != len(self.file_ids):
            raise ValueError("granted file IDs must be unique")
        return self


class TrainingArtifactGrantIssued(TrainingWire):
    grant_id: uuid.UUID
    secret: str = Field(min_length=32, max_length=256, repr=False)
    expires_at: AwareDatetime


class TrainingArtifactImportRequest(TrainingWire):
    command_id: uuid.UUID
    artifact_id: uuid.UUID
    manifest_sha256: Digest
    source_node: StudioName
    destination_node: StudioName
    attempt_id: TrainingId
    fence: int = Field(strict=True, ge=1)
    grant_id: uuid.UUID
    grant_secret: str = Field(min_length=32, max_length=256, repr=False)


class TrainingArtifactImportStatus(TrainingWire):
    import_id: uuid.UUID
    artifact_id: uuid.UUID
    manifest_sha256: Digest
    state: Literal["staging", "transferring", "verifying", "verified", "failed", "cancelled"]
    transferred_bytes: int = Field(ge=0)
    verified_manifest: TrainingArtifactManifest | None = None
    reason: TrainingReason | None = None

    @model_validator(mode="after")
    def actual_verification(self) -> TrainingArtifactImportStatus:
        if self.state == "verified" and (
            self.verified_manifest is None
            or self.verified_manifest.artifact_id != self.artifact_id
            or self.verified_manifest.canonical_sha256() != self.manifest_sha256
        ):
            raise ValueError("verified import requires matching complete artifact manifest")
        return self


class TrainingArtifactImportIntent(TrainingWire):
    """Restart journal identity contains no transfer credential."""

    command_id: uuid.UUID
    artifact_id: uuid.UUID
    manifest_sha256: Digest
    source_node: StudioName
    destination_node: StudioName
    attempt_id: TrainingId
    fence: int = Field(strict=True, ge=1)
    grant_id: uuid.UUID


class TrainingArtifactImportJournal(TrainingWire):
    intent: TrainingArtifactImportIntent
    status: TrainingArtifactImportStatus

    @model_validator(mode="after")
    def matching_import(self) -> TrainingArtifactImportJournal:
        if (
            self.status.import_id != self.intent.command_id
            or self.status.artifact_id != self.intent.artifact_id
            or self.status.manifest_sha256 != self.intent.manifest_sha256
        ):
            raise ValueError("artifact journal status differs from its immutable import intent")
        return self


class TrainingArtifactGrantRefresh(TrainingWire):
    command_id: uuid.UUID
    artifact_id: uuid.UUID
    manifest_sha256: Digest
    attempt_id: TrainingId
    fence: int = Field(strict=True, ge=1)
    grant_id: uuid.UUID
    grant_secret: str = Field(min_length=32, max_length=256, repr=False)


class TrainingArtifactVerifyRequest(TrainingWire):
    command_id: uuid.UUID
    manifest_sha256: Digest


class TrainingArtifactDeleteRequest(TrainingWire):
    command_id: uuid.UUID
    manifest_sha256: Digest
    expected_version: int = Field(strict=True, ge=1)
    unreferenced: Literal[True]
    expected_manifest: TrainingArtifactManifest | None = None

    @model_validator(mode="after")
    def exact_cleanup_manifest(self) -> TrainingArtifactDeleteRequest:
        if (
            self.expected_manifest is not None
            and self.expected_manifest.canonical_sha256() != self.manifest_sha256
        ):
            raise ValueError("cleanup manifest must match the authorized immutable digest")
        return self


class TrainingArtifactVerificationReceipt(TrainingWire):
    command_id: uuid.UUID
    artifact_id: uuid.UUID
    manifest_sha256: Digest
    node: StudioName
    verified_bytes: int = Field(ge=1)


class TrainingArtifactDeletionReceipt(TrainingWire):
    command_id: uuid.UUID
    artifact_id: uuid.UUID
    purged: bool


class TrainingAttemptCleanupRequest(TrainingWire):
    """Retired-job cleanup authority; no execution lease, PID or filesystem path."""

    command_id: uuid.UUID
    job_id: TrainingId
    attempt_id: TrainingId
    fence: int = Field(strict=True, ge=1)
    node: StudioName
    prepared_command_id: uuid.UUID


class TrainingAttemptCleanupReceipt(TrainingWire):
    command_id: uuid.UUID
    job_id: TrainingId
    attempt_id: TrainingId
    fence: int = Field(strict=True, ge=1)
    node: StudioName
    purged: bool


class TrainingReconcileExpectation(TrainingWire):
    attempt_id: TrainingId
    fence: int = Field(ge=1)
    spawn_nonce: uuid.UUID
    pid: int | None = Field(default=None, ge=1)
    process_create_time: float | None = Field(default=None, gt=0)


class TrainingReconcileRequest(TrainingWire):
    expected: list[TrainingReconcileExpectation] = Field(max_length=8)


class TrainingReconcileResult(TrainingWire):
    adopted: list[NodeTrainingStatus] = Field(default_factory=list, max_length=8)
    dead: list[TrainingId] = Field(default_factory=list, max_length=8)
    unknown: list[TrainingId] = Field(default_factory=list, max_length=8)
    orphans: list[NodeTrainingStatus] = Field(default_factory=list, max_length=8)


class TrainingRankGrantRequest(TrainingArtifactGrantRequest):
    component: TrainingRankComponentManifest

    @model_validator(mode="after")
    def matching_component(self) -> TrainingRankGrantRequest:
        if (
            self.component.artifact_id != self.artifact_id
            or self.component.canonical_sha256() != self.manifest_sha256
            or self.component.attempt_id != self.attempt_id
            or self.component.fence != self.fence
            or set(self.file_ids) != {f.id for f in self.component.files}
            or self.max_bytes != self.component.total_bytes
            or self.source_node != ("coire-edge-a" if self.component.rank == 0 else "coire-edge-b")
        ):
            raise ValueError("rank grant differs from its exact full-state component")
        return self


class TrainingRankImportRequest(TrainingArtifactImportRequest):
    component: TrainingRankComponentManifest

    @model_validator(mode="after")
    def matching_component(self) -> TrainingRankImportRequest:
        if (
            self.component.artifact_id != self.artifact_id
            or self.component.canonical_sha256() != self.manifest_sha256
            or self.component.attempt_id != self.attempt_id
            or self.component.fence != self.fence
            or self.source_node == self.destination_node
            or self.source_node != ("coire-edge-a" if self.component.rank == 0 else "coire-edge-b")
        ):
            raise ValueError("rank import differs from its exact peer component")
        return self


class TrainingRankVerificationReceipt(TrainingWire):
    command_id: uuid.UUID
    component: TrainingRankComponentManifest
    node: StudioName
    verified_bytes: int = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def complete_verification(self) -> TrainingRankVerificationReceipt:
        if self.verified_bytes != self.component.total_bytes:
            raise ValueError("rank verification must cover every full-state byte")
        return self


class TrainingRankImportStatus(TrainingWire):
    import_id: uuid.UUID
    component: TrainingRankComponentManifest
    state: Literal["staging", "transferring", "verified", "failed", "cancelled"]
    transferred_bytes: int = Field(strict=True, ge=0)
    receipt: TrainingRankVerificationReceipt | None = None
    reason: TrainingReason | None = None

    @model_validator(mode="after")
    def verified_component(self) -> TrainingRankImportStatus:
        if self.state == "verified" and (
            self.receipt is None or self.receipt.component != self.component
        ):
            raise ValueError("verified rank import requires its exact complete receipt")
        return self


class TrainingRankCollection(TrainingCommand):
    artifact_id: uuid.UUID
    components: list[TrainingRankComponentManifest] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def common_components(self) -> TrainingRankCollection:
        if self.world_size != 2 or {c.rank for c in self.components} != {0, 1}:
            raise ValueError("collection must include both distinct ranks")
        first = self.components[0]
        if any(
            c.artifact_id != self.artifact_id
            or c.job_id != self.job_id
            or c.attempt_id != self.attempt_id
            or c.fence != self.fence
            or c.update != first.update
            or c.runtime_sha256 != first.runtime_sha256
            or c.resolved_spec_sha256 != first.resolved_spec_sha256
            for c in self.components
        ):
            raise ValueError("collection must bind a common evaluated update and runtime")
        return self


class NodeTrainingLeaseSnapshot(TrainingWire):
    node: StudioName
    sampled_at: AwareDatetime
    expires_at: AwareDatetime
    active_leases: dict[uuid.UUID, int] = Field(max_length=256)

    @model_validator(mode="after")
    def bounded_snapshot(self) -> NodeTrainingLeaseSnapshot:
        if self.expires_at <= self.sampled_at or any(
            type(v) is not int or v < 0 for v in self.active_leases.values()
        ):
            raise ValueError("lease snapshot requires finite validity and nonnegative counts")
        return self


class DatasetInputGrant(TrainingWire):
    grant_id: uuid.UUID
    node: StudioName
    dataset_id: uuid.UUID
    source_sha256: Digest
    max_bytes: int = Field(strict=True, ge=1, le=256 * 1024**2)
    analysis_id: uuid.UUID | None = None
    attempt_id: TrainingId | None = None
    expires_at: AwareDatetime
    secret: str = Field(min_length=32, max_length=256, repr=False)

    @model_validator(mode="after")
    def exact_execution_scope(self) -> DatasetInputGrant:
        if (self.analysis_id is None) == (self.attempt_id is None):
            raise ValueError("dataset grant must bind exactly one analysis or training attempt")
        return self


class TrainingInputSource(TrainingWire):
    binding: DatasetAnalysisBinding
    split: SplitManifest
    analysis: DatasetAnalysis
    grant: DatasetInputGrant

    @model_validator(mode="after")
    def matching_source(self) -> TrainingInputSource:
        if (
            self.binding.dataset_id != self.split.dataset_id
            or self.analysis.dataset_id != self.split.dataset_id
            or self.grant.dataset_id != self.split.dataset_id
            or self.binding.source_sha256 != self.grant.source_sha256
            or self.split.source_sha256 != self.grant.source_sha256
            or self.analysis.model_id != self.binding.model_id
            or self.analysis.variant_id != self.binding.variant_id
            or self.analysis.state != "succeeded"
        ):
            raise ValueError("input source must bind its successful analysis, split and grant")
        return self


class TrainingInputsRequest(TrainingCommand):
    sources: list[TrainingInputSource] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def scoped_sources(self) -> TrainingInputsRequest:
        if len({s.binding.dataset_id for s in self.sources}) != len(self.sources):
            raise ValueError("input source identities must be unique")
        if any(
            s.grant.node != self.node or s.grant.attempt_id != self.attempt_id for s in self.sources
        ):
            raise ValueError("input grants must bind the addressed training attempt")
        return self


class TrainingAdapterExtractRequest(TrainingWire):
    command_id: uuid.UUID
    adapter_id: uuid.UUID
    checkpoint_id: uuid.UUID
    checkpoint_manifest_sha256: Digest
    job_id: TrainingId
    attempt_id: TrainingId
    fence: int = Field(strict=True, ge=1)
    node: StudioName
    resolved: ResolvedTrainingSpec
    disk_reservation_id: uuid.UUID
    max_bytes: int = Field(strict=True, ge=1, le=20 * 1024**3)
    deadline: AwareDatetime


class TrainingAdapterExtractionStatus(TrainingWire):
    command_id: uuid.UUID
    adapter_id: uuid.UUID
    checkpoint_id: uuid.UUID
    node: StudioName
    state: Literal["queued", "running", "succeeded", "failed"]
    manifest: TrainingArtifactManifest | None = None
    reason: TrainingReason | None = None

    @model_validator(mode="after")
    def extracted_artifact(self) -> TrainingAdapterExtractionStatus:
        if self.state == "succeeded" and (
            self.manifest is None
            or self.manifest.kind != "adapter"
            or self.manifest.artifact_id != self.adapter_id
        ):
            raise ValueError(
                "successful extraction requires the reserved immutable adapter artifact"
            )
        return self


class NodeDatasetAnalysisRequest(TrainingWire):
    command_id: uuid.UUID
    request_sha256: Digest
    analysis_id: uuid.UUID
    model_id: uuid.UUID
    variant_id: uuid.UUID
    base_manifest_sha256: Digest
    template_sha256: Digest | None = None
    input_grant: DatasetInputGrant
    reservation_id: uuid.UUID
    memory_bytes: int = Field(strict=True, ge=1, le=1024**3)
    max_sequence_length: int = Field(default=8192, strict=True, ge=2, le=8192)
    deadline: AwareDatetime
    binding: DatasetAnalysisBinding

    @model_validator(mode="after")
    def matching_analysis(self) -> NodeDatasetAnalysisRequest:
        if self.input_grant.analysis_id != self.analysis_id:
            raise ValueError("analysis grant scope differs from requested analysis")
        if (
            self.binding.model_id != self.model_id
            or self.binding.variant_id != self.variant_id
            or self.binding.base_manifest_sha256 != self.base_manifest_sha256
            or self.binding.dataset_id != self.input_grant.dataset_id
            or self.binding.source_sha256 != self.input_grant.source_sha256
        ):
            raise ValueError("analysis binding differs from model or input grant")
        if (
            self.binding.template_override is not None
            and self.template_sha256
            != hashlib.sha256(self.binding.template_override.encode()).hexdigest()
        ):
            raise ValueError("analysis override digest differs from frozen template content")
        return self


class NodeAnalysisReceipt(TrainingWire):
    analysis_id: uuid.UUID
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]


class NodeDatasetAnalysisStatus(NodeAnalysisReceipt):
    completed_rows: int = Field(ge=0, le=1_000_000)
    result: DatasetAnalysis | None = None
    reason: TrainingReason | None = None


class NodeAnalysisCancelRequest(TrainingWire):
    command_id: uuid.UUID
    analysis_id: uuid.UUID


class DatasetAnalysisWorkerInput(TrainingWire):
    command_id: uuid.UUID
    analysis_id: uuid.UUID
    binding: DatasetAnalysisBinding
    source_bytes: int = Field(strict=True, ge=1, le=256 * 1024**2)
    memory_bytes: int = Field(strict=True, ge=1, le=1024**3)
    max_sequence_length: int = Field(default=8192, strict=True, ge=2, le=8192)
    deadline: AwareDatetime


class NodeProgressPayload(TrainingWire):
    kind: Literal["progress"] = "progress"
    metric: TrainingMetricSample


class NodeCheckpointPayload(TrainingWire):
    kind: Literal["checkpoint_staged"] = "checkpoint_staged"
    manifest: TrainingArtifactManifest


class NodeControlPayload(TrainingWire):
    kind: Literal["pause_requested", "failure", "stopped"]
    reason: TrainingReason


class NodeRankCheckpointPayload(TrainingWire):
    kind: Literal["checkpoint_rank_staged"] = "checkpoint_rank_staged"
    component: TrainingRankComponentManifest


type NodeTrainingPayload = Annotated[
    NodeProgressPayload | NodeCheckpointPayload | NodeRankCheckpointPayload | NodeControlPayload,
    Field(discriminator="kind"),
]


class NodeTrainingEvent(TrainingWire):
    sequence: int = Field(ge=1)
    job_id: TrainingId
    attempt_id: TrainingId
    fence: int = Field(ge=1)
    update: int = Field(ge=0, le=100_000)
    recorded_at: AwareDatetime
    payload: NodeTrainingPayload


class NodeTrainingEventPage(TrainingWire):
    items: list[NodeTrainingEvent] = Field(max_length=100)
    next_sequence: int = Field(ge=0)


class SingleSourceSamplerState(TrainingWire):
    algorithm: Literal["coire-single-source-v1"] = "coire-single-source-v1"
    dataset_sha256: Digest
    row_count: int = Field(strict=True, ge=1, le=1_000_000)
    batch_size: int = Field(strict=True, ge=1, le=64)
    max_sequence_length: int = Field(strict=True, ge=2, le=8192)
    epoch: int = Field(strict=True, ge=0)
    cursor: int = Field(strict=True, ge=0, le=1_000_000)
    permutation: list[int] = Field(min_length=1, max_length=1_000_000)
    rng_version: Literal[3] = 3
    rng_state: list[int] = Field(min_length=625, max_length=625)
    gaussian_cache: float | None = None

    @model_validator(mode="after")
    def complete_sampler_state(self) -> SingleSourceSamplerState:
        if (
            self.cursor > self.row_count
            or self.cursor % self.batch_size
            or self.row_count % self.batch_size
        ):
            raise ValueError("sampler cursor must identify a complete batch boundary")
        if len(self.permutation) != self.row_count or set(self.permutation) != set(
            range(self.row_count)
        ):
            raise ValueError("sampler permutation must contain each row once")
        if (
            any(type(value) is not int or not 0 <= value < 2**32 for value in self.rng_state)
            or self.rng_state[-1] > 624
        ):
            raise ValueError("sampler random state is invalid")
        return self


class MixtureSamplerState(TrainingWire):
    algorithm: Literal["coire-mixture-v1"] = "coire-mixture-v1"
    identity_sha256: Digest
    epoch: int = Field(strict=True, ge=0, lt=2**64)
    cursor: int = Field(strict=True, ge=0, le=16_000_000)
    rank: int = Field(strict=True, ge=0, le=1)
    world_size: Literal[1, 2]
    generator_version: Literal["sha256-counter-v1"] = "sha256-counter-v1"

    @model_validator(mode="after")
    def rank_matches(self) -> MixtureSamplerState:
        if self.rank >= self.world_size:
            raise ValueError("sampler rank must be below world size")
        return self


class SftBatch(TrainingWire):
    tokens: list[list[int]] = Field(min_length=1, max_length=64)
    target_masks: list[list[bool]] = Field(min_length=1, max_length=64)
    source_rows: list[int] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def aligned_batch(self) -> SftBatch:
        if len(self.tokens) != len(self.target_masks) or len(self.tokens) != len(self.source_rows):
            raise ValueError("SFT batch rows and masks must align")
        widths = {len(row) for row in self.tokens}
        if len(widths) != 1 or not 2 <= next(iter(widths)) <= 8192:
            raise ValueError("SFT batch must be rectangular and bounded")
        for tokens, mask in zip(self.tokens, self.target_masks, strict=True):
            if len(tokens) != len(mask) or mask[0] or not any(mask[1:]):
                raise ValueError("every SFT row needs aligned supervised targets")
            if any(type(token) is not int or not 0 <= token < 2**31 for token in tokens):
                raise ValueError("SFT tokens must be bounded int32 values")
        return self


class CheckpointWorkerState(TrainingWire):
    schema_version: Literal[1] = 1
    job_id: TrainingId
    attempt_id: TrainingId
    fence: int = Field(strict=True, ge=1)
    completed_update: int = Field(strict=True, ge=0, le=100_000)
    rank: int = Field(strict=True, ge=0, le=1)
    world_size: Literal[1, 2]
    runtime_sha256: Digest
    resolved_spec_sha256: Digest
    optimizer: TrainingOptimizer
    mlx_rng_key: tuple[int, int]
    sampler: SingleSourceSamplerState | MixtureSamplerState
    optimizer_tree: JsonValue = None

    @model_validator(mode="after")
    def resumable_boundary(self) -> CheckpointWorkerState:
        if self.rank >= self.world_size or self.completed_update > self.optimizer.updates:
            raise ValueError("checkpoint rank/update differs from its declared execution")
        if any(type(word) is not int or not 0 <= word < 2**32 for word in self.mlx_rng_key):
            raise ValueError("checkpoint MLX key must contain two uint32 words")
        return self
