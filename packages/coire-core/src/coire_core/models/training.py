"""Versioned SFT intent, durable jobs, progress and measurement contracts."""

from __future__ import annotations

import hashlib
import json
import uuid
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BeforeValidator,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from coire_core.models.adapters import InferenceTarget
from coire_core.models.datasets import DatasetMixture, DatasetValidationSources
from coire_core.models.evaluation import EvaluationTarget
from coire_core.models.evaluation_links import EvaluationGroupLink
from coire_core.models.training_evaluation import ResolvedTrainingSuite, TrainingSuiteSchedule
from coire_core.models.training_types import (
    AdapterSlug,
    Digest,
    Seed,
    StudioName,
    TrainingId,
    TrainingWire,
)


class TrainingModel(TrainingWire):
    model_id: uuid.UUID
    variant_id: uuid.UUID


class TrainingData(TrainingWire):
    train: DatasetMixture
    validation: DatasetValidationSources
    loss_policy: Literal["all_tokens", "final_assistant"] = "final_assistant"


class TrainingParameterization(TrainingWire):
    kind: Literal["lora", "qlora", "dora"] = "lora"
    rank: int = Field(default=8, strict=True, ge=1, le=128)
    scale: float = Field(default=20.0, strict=True, gt=0, le=1024)
    dropout: float = Field(default=0.0, strict=True, ge=0, lt=1)
    target_modules: list[
        Annotated[
            str,
            Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$", max_length=128),
        ]
    ] = Field(min_length=1, max_length=32)
    num_layers: int = Field(default=1, strict=True, ge=1, le=256)

    @model_validator(mode="after")
    def unique_targets(self) -> TrainingParameterization:
        if len(set(self.target_modules)) != len(self.target_modules):
            raise ValueError("training target modules must be unique")
        return self


class LearningRateSchedule(TrainingWire):
    kind: Literal["constant", "warmup_linear"] = "constant"
    warmup_updates: int = Field(default=0, strict=True, ge=0, le=100_000)

    @model_validator(mode="after")
    def explicit_warmup(self) -> LearningRateSchedule:
        if self.kind == "constant" and self.warmup_updates:
            raise ValueError("constant schedule cannot declare warmup updates")
        return self


class TrainingOptimizer(TrainingWire):
    name: Literal["adam", "adamw"] = "adamw"
    learning_rate: float = Field(default=1e-5, strict=True, gt=0, le=1)
    beta1: float = Field(default=0.9, strict=True, ge=0, lt=1)
    beta2: float = Field(default=0.999, strict=True, ge=0, lt=1)
    epsilon: float = Field(default=1e-8, strict=True, gt=0, le=1)
    weight_decay: float = Field(default=0.0, strict=True, ge=0, le=1)
    updates: int = Field(strict=True, ge=1, le=100_000)
    batch_size: int = Field(default=1, strict=True, ge=1, le=64)
    accumulation_steps: int = Field(default=1, strict=True, ge=1, le=64)
    max_sequence_length: int = Field(default=2048, strict=True, ge=2, le=8192)
    schedule: LearningRateSchedule = Field(default_factory=LearningRateSchedule)

    @model_validator(mode="after")
    def optimizer_schedule(self) -> TrainingOptimizer:
        if self.schedule.warmup_updates > self.updates:
            raise ValueError("warmup cannot exceed completed training updates")
        if self.name == "adam" and self.weight_decay:
            raise ValueError("weight decay requires adamw")
        return self


class TrainingEvaluation(TrainingWire):
    loss_every_updates: int = Field(default=100, strict=True, ge=1, le=100_000)
    at_end: bool = True


class TrainingOutput(TrainingWire):
    adapter_slug: AdapterSlug
    checkpoint_every_updates: int = Field(default=100, strict=True, ge=1, le=100_000)
    keep_last_checkpoints: int = Field(default=3, strict=True, ge=1, le=3)


class TrainingPlacement(TrainingWire):
    mode: Literal["single", "data_parallel"] = "single"
    preferred_node: StudioName | None = None

    @model_validator(mode="after")
    def declared_placement(self) -> TrainingPlacement:
        if self.mode == "data_parallel" and self.preferred_node is not None:
            raise ValueError("data-parallel placement uses both declared Studios")
        return self


class TrainingSpec(TrainingWire):
    schema_version: Literal[1] = 1
    model: TrainingModel
    data: TrainingData
    objective: Literal["sft"] = "sft"
    parameterization: TrainingParameterization
    optim: TrainingOptimizer
    eval: TrainingEvaluation = Field(default_factory=TrainingEvaluation)
    output: TrainingOutput
    placement: TrainingPlacement = Field(default_factory=TrainingPlacement)
    seed: Seed = 0

    @model_validator(mode="after")
    def complete_global_batches(self) -> TrainingSpec:
        if self.placement.mode == "data_parallel" and self.optim.batch_size % 2:
            raise ValueError("data-parallel global batch must be divisible by two")
        if self.data.train.epoch_samples % self.optim.batch_size:
            raise ValueError("epoch samples must be divisible by global batch size")
        return self

    def canonical_sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()


class TrainingEvaluationV2(TrainingEvaluation):
    suites: list[TrainingSuiteSchedule] = Field(min_length=1, max_length=4)


class TrainingSpecV2(TrainingWire):
    schema_version: Literal[2] = 2
    model: TrainingModel
    data: TrainingData
    objective: Literal["sft"] = "sft"
    parameterization: TrainingParameterization
    optim: TrainingOptimizer
    eval: TrainingEvaluationV2
    output: TrainingOutput
    placement: TrainingPlacement = Field(default_factory=TrainingPlacement)
    seed: Seed = 0

    @model_validator(mode="after")
    def complete_global_batches(self) -> TrainingSpecV2:
        if self.placement.mode == "data_parallel" and self.optim.batch_size % 2:
            raise ValueError("data-parallel global batch must be divisible by two")
        if self.data.train.epoch_samples % self.optim.batch_size:
            raise ValueError("epoch samples must be divisible by global batch size")
        schedules = self.eval.suites
        if len({(item.suite_id, item.suite_version) for item in schedules}) != len(schedules):
            raise ValueError("training evaluation suites must be unique")
        updates = {update for item in schedules for update in item.checkpoint_updates}
        if len(updates) > 32 or any(
            update >= self.optim.updates or update % self.output.checkpoint_every_updates
            for update in updates
        ):
            raise ValueError("suite checkpoints must be recoverable pre-final updates")
        return self

    def canonical_sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()


def _default_training_version(value: object) -> object:
    if isinstance(value, dict) and "schema_version" not in value:
        return {**value, "schema_version": 1}
    return value


def _numeric_version_schema(schema: dict[str, object]) -> None:
    """Keep numeric version constants without string-only OpenAPI mappings."""
    schema.pop("discriminator", None)


type TrainingSpecDocument = Annotated[
    TrainingSpec | TrainingSpecV2,
    Field(discriminator="schema_version", json_schema_extra=_numeric_version_schema),
    BeforeValidator(_default_training_version),
]


def parse_training_spec(value: object) -> TrainingSpec | TrainingSpecV2:
    return TypeAdapter(TrainingSpecDocument).validate_python(value)


class TrainingSubmission(TrainingWire):
    source_yaml: str = Field(min_length=1, max_length=65536)
    source_kind: Literal["yaml", "form"] = "yaml"
    form_spec: TrainingSpecDocument | None = None
    preview_sha256: Digest | None = None

    @field_validator("source_yaml")
    @classmethod
    def source_byte_limit(cls, value: str) -> str:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeError as error:
            raise ValueError("recipe must be valid UTF-8") from error
        if len(encoded) > 64 * 1024 or "\x00" in value:
            raise ValueError("recipe exceeds its byte bound or contains NUL")
        return value

    @model_validator(mode="after")
    def explicit_form_source(self) -> TrainingSubmission:
        if (self.source_kind == "form") != (self.form_spec is not None):
            raise ValueError("form submissions require form_spec; YAML submissions do not")
        return self


class ParsedTrainingSubmission(TrainingWire):
    source_yaml: str = Field(min_length=1, max_length=65536)
    source_kind: Literal["yaml", "form"]
    source_sha256: Digest
    intent_sha256: Digest
    spec: TrainingSpecDocument


class ResolvedDatasetInput(TrainingWire):
    dataset_id: uuid.UUID
    analysis_id: uuid.UUID
    source_sha256: Digest
    split_sha256: Digest
    analysis_sha256: Digest


class TrainingResourceEnvelope(TrainingWire):
    weight_bytes: int = Field(ge=1)
    adapter_bytes: int = Field(ge=1)
    optimizer_bytes: int = Field(ge=1)
    activation_bytes: int = Field(ge=1)
    buffer_bytes: int = Field(ge=0)
    safety_bytes: int = Field(ge=1)
    checkpoint_bytes: int = Field(ge=1)
    evidence_sha256: Digest

    @property
    def memory_bytes(self) -> int:
        return (
            self.weight_bytes
            + self.adapter_bytes
            + self.optimizer_bytes
            + self.activation_bytes
            + self.buffer_bytes
            + self.safety_bytes
        )


class ResolvedTrainingSpec(TrainingWire):
    spec: TrainingSpec
    base_manifest_sha256: Digest
    datasets: list[ResolvedDatasetInput] = Field(min_length=1, max_length=32)
    tokenizer_sha256: Digest
    template_sha256: Digest
    enable_thinking: bool
    runtime_sha256: Digest
    worker_version: str = Field(min_length=1, max_length=64)
    sampler_version: Literal["coire-sampler-v1"] = "coire-sampler-v1"
    resource_envelope: TrainingResourceEnvelope


class ResolvedTrainingSpecV2(TrainingWire):
    spec: TrainingSpecV2
    base_manifest_sha256: Digest
    datasets: list[ResolvedDatasetInput] = Field(min_length=1, max_length=32)
    tokenizer_sha256: Digest
    template_sha256: Digest
    enable_thinking: bool
    runtime_sha256: Digest
    worker_version: str = Field(min_length=1, max_length=64)
    sampler_version: Literal["coire-sampler-v1"] = "coire-sampler-v1"
    resource_envelope: TrainingResourceEnvelope

    evaluations: list[ResolvedTrainingSuite] = Field(min_length=1, max_length=4)
    evaluation_base: EvaluationTarget

    @model_validator(mode="after")
    def complete_suites(self) -> ResolvedTrainingSpecV2:
        if [item.schedule for item in self.evaluations] != self.spec.eval.suites:
            raise ValueError("resolved evaluation schedule differs from immutable intent")
        base = self.evaluation_base
        if (
            base.target.model_id != self.spec.model.model_id
            or base.target.variant_id != self.spec.model.variant_id
            or base.target.adapter_id is not None
            or base.target.base_manifest_sha256 != self.base_manifest_sha256
            or base.runtime.tokenizer_sha256 != self.tokenizer_sha256
            or base.runtime.template_sha256 != self.template_sha256
        ):
            raise ValueError("evaluation base differs from the frozen training identity")
        return self


type ResolvedTrainingSpecDocument = ResolvedTrainingSpec | ResolvedTrainingSpecV2


def parse_resolved_training_spec(value: object) -> ResolvedTrainingSpecDocument:
    return TypeAdapter(ResolvedTrainingSpecDocument).validate_python(value)


class TrainingJobState(StrEnum):
    QUEUED = "queued"
    PREFLIGHTING = "preflighting"
    RESERVING = "reserving"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    RECOVERING = "recovering"
    FINALIZING = "finalizing"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TRAINING_STATES = frozenset(
    {TrainingJobState.SUCCEEDED, TrainingJobState.FAILED, TrainingJobState.CANCELLED}
)
type TrainingReason = Literal[
    "capacity_busy",
    "impossible_fit",
    "analysis_pending",
    "invalid_input",
    "unauthorized",
    "profile_missing",
    "profile_expired",
    "insufficient_samples",
    "latency_breach",
    "thermal_breach",
    "memory_breach",
    "lease_expired",
    "node_unreachable",
    "rank_failed",
    "checkpoint_invalid",
    "replication_failed",
    "disk_full",
    "runtime_mismatch",
    "queue_timeout",
    "execution_timeout",
    "recovery_exhausted",
    "admin_pause",
    "evaluation_pending",
    "cancelled",
    "internal",
]


class TrainingJobReceipt(TrainingWire):
    job_id: TrainingId
    version: int = Field(ge=1)
    state: TrainingJobState
    events_path: str = Field(pattern=r"^/api/v1/admin/training/jobs/[0-9A-HJKMNP-TV-Z]{26}/events$")


class TrainingAttempt(TrainingWire):
    id: TrainingId
    job_id: TrainingId
    generation: int = Field(strict=True, ge=1)
    fence: int = Field(strict=True, ge=1)
    world_size: Literal[1, 2]
    runtime_sha256: Digest
    resume_checkpoint_id: uuid.UUID | None = None
    state: Literal["preparing", "running", "stopping", "stopped", "unknown", "failed"]
    lease_expires_at: AwareDatetime
    created_at: AwareDatetime
    stopped_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def terminal_observation(self) -> TrainingAttempt:
        if self.state == "stopped" and self.stopped_at is None:
            raise ValueError("stopped attempt requires termination observation")
        return self


class TrainingControlRequest(TrainingWire):
    expected_version: int = Field(strict=True, ge=1)


class TrainingCommandReceipt(TrainingWire):
    command_id: uuid.UUID
    job_id: TrainingId
    state: TrainingJobState
    version: int = Field(ge=1)


class TrainingMetricSample(TrainingWire):
    job_id: TrainingId
    attempt_id: TrainingId
    update: int = Field(ge=0, le=100_000)
    kind: Literal["train", "validation"]
    loss: float
    learning_rate: float = Field(ge=0)
    tokens: int = Field(ge=0)
    tokens_per_second: float = Field(ge=0)
    updates_per_second: float = Field(ge=0)
    footprint_bytes: int = Field(ge=0)
    peak_bytes: int = Field(ge=0)
    rolled_back: bool = False
    recorded_at: AwareDatetime


class TrainingMetricPage(TrainingWire):
    items: list[TrainingMetricSample] = Field(max_length=2000)
    next_cursor: str | None = Field(default=None, max_length=512)


class TrainingJobDetail(TrainingWire):
    evaluation_groups: list[EvaluationGroupLink] = Field(
        default_factory=list, max_length=100, exclude_if=lambda value: not value
    )
    id: TrainingId
    version: int = Field(ge=1)
    state: TrainingJobState
    source_yaml: str = Field(max_length=65536)
    source_sha256: Digest
    intent_sha256: Digest
    spec: TrainingSpecDocument
    resolved: ResolvedTrainingSpecDocument | None = None
    attempt_id: TrainingId | None = None
    completed_update: int = Field(default=0, ge=0, le=100_000)
    latest_checkpoint_id: uuid.UUID | None = None
    adapter_id: uuid.UUID | None = None
    reason: TrainingReason | None = None
    reproducible: bool = True
    created_at: AwareDatetime
    updated_at: AwareDatetime


class TrainingJobPage(TrainingWire):
    items: list[TrainingJobDetail] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)


class TrainingStateEvent(TrainingWire):
    kind: Literal["state", "terminal"]
    state: TrainingJobState
    reason: TrainingReason | None = None


class TrainingProgressEvent(TrainingWire):
    kind: Literal["progress"] = "progress"
    metric: TrainingMetricSample


class TrainingCheckpointEvent(TrainingWire):
    kind: Literal["checkpoint"] = "checkpoint"
    checkpoint_id: uuid.UUID
    update: int = Field(ge=0)
    manifest_sha256: Digest


class TrainingRecoveryEvent(TrainingWire):
    kind: Literal["recovery"] = "recovery"
    reason: TrainingReason
    resume_update: int = Field(ge=0)


class TrainingJobSnapshot(TrainingWire):
    """Content-free reconnect state; original recipes remain on the authenticated detail route."""

    id: TrainingId
    version: int = Field(ge=1)
    state: TrainingJobState
    completed_update: int = Field(ge=0)
    latest_checkpoint_id: uuid.UUID | None = None
    adapter_id: uuid.UUID | None = None
    reason: TrainingReason | None = None


class TrainingResetEvent(TrainingWire):
    kind: Literal["reset"] = "reset"
    snapshot: TrainingJobSnapshot


type TrainingEventPayload = Annotated[
    TrainingStateEvent
    | TrainingProgressEvent
    | TrainingCheckpointEvent
    | TrainingRecoveryEvent
    | TrainingResetEvent,
    Field(discriminator="kind"),
]


class TrainingEvent(TrainingWire):
    id: int = Field(ge=1)
    job_id: TrainingId
    attempt_id: TrainingId | None = None
    state_version: int = Field(ge=1)
    occurred_at: AwareDatetime
    kind: Literal["state", "progress", "checkpoint", "recovery", "terminal", "reset"]
    payload: TrainingEventPayload

    @model_validator(mode="after")
    def consistent_payload(self) -> TrainingEvent:
        if self.kind != self.payload.kind:
            raise ValueError("training event kind must match its typed payload")
        if isinstance(self.payload, TrainingProgressEvent) and (
            self.payload.metric.job_id != self.job_id
            or self.payload.metric.attempt_id != self.attempt_id
        ):
            raise ValueError("progress metric differs from event job/attempt identity")
        return self


class TrainingReplayPage(TrainingWire):
    events: list[TrainingEvent] = Field(max_length=100)
    cursor: int = Field(ge=0)
    reset: TrainingJobSnapshot | None = None


class TrainingMetricCursor(TrainingWire):
    job_id: TrainingId
    recorded_at: AwareDatetime
    id: uuid.UUID


class TrainingValidation(TrainingWire):
    spec: TrainingSpecDocument
    intent_sha256: Digest
    resolved: ResolvedTrainingSpecDocument | None = None
    ready_to_run: bool = False
    reasons: list[TrainingReason] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def complete_validation(self) -> TrainingValidation:
        if self.ready_to_run and (self.resolved is None or self.reasons):
            raise ValueError("ready validation requires complete resolution and no pending reasons")
        return self


class CheckpointDetail(TrainingWire):
    id: uuid.UUID
    job_id: TrainingId
    attempt_id: TrainingId
    fence: int = Field(ge=1)
    update: int = Field(ge=0)
    manifest_sha256: Digest
    total_bytes: int = Field(ge=1)
    state: Literal["staging", "replicating", "committed", "corrupt", "purging", "purged"]
    verified_nodes: list[StudioName] = Field(default_factory=list, max_length=2)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def mirrored_commit(self) -> CheckpointDetail:
        if len(set(self.verified_nodes)) != len(self.verified_nodes):
            raise ValueError("checkpoint copy nodes must be unique")
        if self.state == "committed" and set(self.verified_nodes) != {
            "coire-edge-a",
            "coire-edge-b",
        }:
            raise ValueError("durable checkpoint requires both verified Studio copies")
        return self


class CheckpointPage(TrainingWire):
    items: list[CheckpointDetail] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)


class CheckpointPromotionRequest(TrainingWire):
    expected_version: int = Field(strict=True, ge=1)
    adapter_slug: AdapterSlug


class TrainingDeleteRequest(TrainingControlRequest):
    pass


class TrainingDeletionReceipt(TrainingWire):
    job_id: TrainingId
    state: Literal["retired", "purged"]


class TrainingRecipe(TrainingWire):
    id: AdapterSlug
    version: int = Field(ge=1)
    parameterization: Literal["lora", "qlora", "dora"]
    template_yaml: str = Field(max_length=65536)
    required_bindings: list[
        Literal[
            "model_id",
            "variant_id",
            "dataset_id",
            "adapter_slug",
            "task_suite_id",
            "judge_suite_id",
        ]
    ] = Field(min_length=1, max_length=6)


class TrainingRecipePage(TrainingWire):
    items: list[TrainingRecipe] = Field(max_length=100)


class TrainingMeasurementWorkload(TrainingWire):
    sha256: Digest
    query_version: Literal["coire-ttft-v1"] = "coire-ttft-v1"
    concurrency_per_target: int = Field(strict=True, ge=1, le=16)
    arrival_interval_ms: int = Field(strict=True, ge=1, le=60_000)
    max_input_tokens: int = Field(strict=True, ge=4000, le=4000, default=4000)
    max_output_tokens: int = Field(strict=True, ge=1, le=1024)


class TrainingResidentTarget(TrainingWire):
    instance_id: uuid.UUID
    target: InferenceTarget


class TrainingMeasurementRequest(TrainingWire):
    spec: TrainingSpec
    nodes: list[StudioName] = Field(min_length=1, max_length=2)
    resident_targets: list[TrainingResidentTarget] = Field(default_factory=list, max_length=32)
    workload: TrainingMeasurementWorkload
    mode: Literal["memory", "coexistence"]
    prompts: TrainingMeasurementPromptSet | None = None

    @model_validator(mode="after")
    def measurement_identity(self) -> TrainingMeasurementRequest:
        if len(set(self.nodes)) != len(self.nodes):
            raise ValueError("measurement nodes must be unique")
        expected = 2 if self.spec.placement.mode == "data_parallel" else 1
        if len(self.nodes) != expected:
            raise ValueError("measurement nodes must match training world size")
        if self.mode == "coexistence" and not self.resident_targets:
            raise ValueError("coexistence measurement needs declared resident targets")
        if len({item.instance_id for item in self.resident_targets}) != len(self.resident_targets):
            raise ValueError("resident instance identities must be unique")
        return self


class TargetLatencyMeasurement(TrainingWire):
    instance_id: uuid.UUID
    baseline_requests: int = Field(ge=0)
    mixed_requests: int = Field(ge=0)
    baseline_p95_seconds: float = Field(ge=0)
    mixed_p95_seconds: float = Field(ge=0)


class TrainingMeasurementReceipt(TrainingWire):
    measurement_id: uuid.UUID
    state: Literal["queued", "running", "succeeded", "failed", "inconclusive"]


class TrainingNodeMemoryEvidence(TrainingWire):
    node: StudioName
    hardware_sha256: Digest
    peak_footprint_bytes: int = Field(strict=True, ge=1)
    swap_growth_bytes: int = Field(strict=True, ge=0)
    thermal_ok: bool


class TrainingMemoryEvidence(TrainingWire):
    measurement_id: uuid.UUID
    training_config_sha256: Digest
    base_manifest_sha256: Digest
    tokenizer_sha256: Digest
    template_sha256: Digest
    runtime_sha256: Digest
    worker_version: str = Field(min_length=1, max_length=64)
    completed_updates: int = Field(strict=True, ge=1, le=100_000)
    resource_envelope: TrainingResourceEnvelope
    nodes: list[TrainingNodeMemoryEvidence] = Field(min_length=1, max_length=2)
    measured_at: AwareDatetime
    valid_until: AwareDatetime

    @model_validator(mode="after")
    def bounded_measured_evidence(self) -> TrainingMemoryEvidence:
        if self.valid_until <= self.measured_at or len({n.node for n in self.nodes}) != len(
            self.nodes
        ):
            raise ValueError("memory evidence requires unique nodes and positive validity")
        return self


class TrainingMeasurementResult(TrainingWire):
    id: uuid.UUID
    request: TrainingMeasurementRequest
    state: Literal["queued", "running", "succeeded", "failed", "inconclusive"]
    targets: list[TargetLatencyMeasurement] = Field(default_factory=list, max_length=32)
    report_sha256: Digest | None = None
    profile_id: uuid.UUID | None = None
    completed_updates: int = Field(default=0, ge=0)
    swap_growth_bytes: int = Field(default=0, ge=0)
    peak_memory_bytes: int = Field(default=0, ge=0)
    thermal_ok: bool = False
    created_at: AwareDatetime
    memory_evidence: TrainingMemoryEvidence | None = None
    phases: list[TrainingMeasurementPhase] = Field(default_factory=list, max_length=2)
    node_report_sha256: list[Digest] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def approval_requires_complete_evidence(self) -> TrainingMeasurementResult:
        if self.profile_id is not None and self.state != "succeeded":
            raise ValueError("only successful measurement may approve a profile")
        if self.state == "succeeded":
            if (
                self.report_sha256 is None
                or self.completed_updates < 1
                or self.swap_growth_bytes
                or not self.thermal_ok
            ):
                raise ValueError("successful measurement requires safe recorded training progress")
            if self.request.mode == "coexistence":
                expected = {item.instance_id for item in self.request.resident_targets}
                if {item.instance_id for item in self.targets} != expected or len(
                    self.targets
                ) != len(expected):
                    raise ValueError("measurement must cover every declared target")
                if any(
                    item.baseline_requests < 100
                    or item.mixed_requests < 100
                    or item.baseline_p95_seconds > 1.5
                    or item.mixed_p95_seconds > 1.5
                    for item in self.targets
                ):
                    raise ValueError(
                        "profile approval requires sampled passing latency for every target"
                    )
        return self


class TrainingProfile(TrainingWire):
    id: uuid.UUID
    report_sha256: Digest
    request: TrainingMeasurementRequest
    expires_at: AwareDatetime
    invalidated_reason: TrainingReason | None = None


class TrainingProfilePage(TrainingWire):
    items: list[TrainingProfile] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)


class TrainingMeasurementPrompt(TrainingWire):
    text: str = Field(min_length=1, max_length=16384)
    input_tokens: int = Field(strict=True, ge=1, le=4000)
    tokens_by_instance: dict[uuid.UUID, Annotated[int, Field(strict=True, ge=1, le=4000)]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def bounded_target_lengths(self) -> TrainingMeasurementPrompt:
        if len(self.tokens_by_instance) > 32:
            raise ValueError("prompt token metadata exceeds resident target bound")
        return self


class TrainingMeasurementPromptSet(TrainingWire):
    """Studio-tokenized workload; core retains bounded text, never a tokenizer."""

    prompts: list[TrainingMeasurementPrompt] = Field(min_length=1, max_length=32)

    def canonical_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()


class TrainingMeasurementPhase(TrainingWire):
    phase: Literal["baseline", "mixed"]
    workload_sha256: Digest
    started_at: AwareDatetime
    finished_at: AwareDatetime
    samples: dict[uuid.UUID, list[Annotated[float, Field(ge=0, allow_inf_nan=False)]]]
    failures: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def bounded_samples(self) -> TrainingMeasurementPhase:
        if len(self.samples) > 32 or any(len(v) > 20000 for v in self.samples.values()):
            raise ValueError("phase samples exceed bounded report")
        if self.finished_at < self.started_at:
            raise ValueError("phase timestamps are reversed")
        return self


class TrainingMeasurementCompletion(TrainingWire):
    """Authenticated gateway reports the instance that actually completed the stream."""

    instance_id: uuid.UUID
    target: InferenceTarget
    first_token_seconds: float = Field(ge=0, allow_inf_nan=False)
    input_tokens: int = Field(strict=True, ge=1, le=4000)
    output_tokens: int = Field(strict=True, ge=1, le=1024)


TrainingMeasurementResult.model_rebuild()
TrainingMeasurementRequest.model_rebuild()
