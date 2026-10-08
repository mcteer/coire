"""Strict evaluation catalog, execution, evidence and Studio transport contracts."""

from __future__ import annotations

import hashlib
import json
import uuid
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from coire_core.models.adapters import InferenceTarget, ModelSelector, validate_transport_target
from coire_core.models.evaluation_inputs import EvaluationTrainingBinding
from coire_core.models.harness import CategoryScores, EvaluationVerdict
from coire_core.models.registry import CapabilityProfile, EngineBackend
from coire_core.models.training_types import AdapterSlug, Digest, Seed, StudioName, TrainingId

MAX_EVALUATION_BYTES = 8 * 1024**2
MAX_CASE_BYTES = 16 * 1024


def canonical_digest(value: BaseModel) -> str:
    """Hash a finite normalized wire document independently of JSON presentation."""
    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class EvaluationWire(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SuiteKind(StrEnum):
    HARNESS = "harness"
    TASK = "task"
    JUDGE = "judge"


class SuiteMode(StrEnum):
    CAPABILITY = "capability"
    DETERMINISTIC = "deterministic"
    RUBRIC = "rubric"
    PAIRWISE = "pairwise"


class EvaluationState(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RESERVING = "reserving"
    RUNNING = "running"
    COLLECTING = "collecting"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


TERMINAL_EVALUATION_STATES = frozenset(
    {
        EvaluationState.SUCCEEDED,
        EvaluationState.FAILED,
        EvaluationState.TIMED_OUT,
        EvaluationState.CANCELLED,
    }
)
type EvaluationPhase = Literal["base", "candidate", "judge", "harness", "contamination", "cleanup"]
type EvaluationReason = Literal[
    "model_unavailable",
    "judge_unavailable",
    "authorization_revoked",
    "malformed_judge",
    "invalid_evidence",
    "runtime_mismatch",
    "evidence_unavailable",
    "capacity_busy",
    "capacity_timeout",
    "admission_disabled",
    "execution_timeout",
    "latency_breach",
    "memory_breach",
    "thermal_breach",
    "telemetry_stale",
    "cancelled",
    "internal",
]
type TemplateId = Literal[
    "harness-capability", "task-coding-instructions", "judge-rubric", "judge-pairwise"
]


class EvaluationGeneration(EvaluationWire):
    temperature: float = Field(default=0, ge=0, le=2)
    top_p: float = Field(default=1, gt=0, le=1)
    seed: Seed = 0
    max_tokens: int = Field(default=512, strict=True, ge=1, le=4096)
    stop: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=4
    )


class EvaluationSubject(EvaluationWire):
    model_id: uuid.UUID
    variant_id: uuid.UUID
    adapter_id: uuid.UUID | None = None


class EvaluationRuntime(EvaluationWire):
    engine_version: str = Field(min_length=1, max_length=64)
    harness_version: str = Field(min_length=1, max_length=64)
    runtime_sha256: Digest
    tokenizer_sha256: Digest
    template_sha256: Digest
    capability_sha256: Digest


class EvaluationTarget(EvaluationWire):
    engine_backend: EngineBackend = Field(
        default=EngineBackend.MLX_LM, exclude_if=lambda value: value is EngineBackend.MLX_LM
    )
    variant_slug: str | None = Field(
        default=None, min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$"
    )
    template_override: str | None = Field(default=None, max_length=64 * 1024)
    target: InferenceTarget
    public_selector: ModelSelector
    capability_profile: CapabilityProfile
    context_window: int = Field(ge=256)
    runtime: EvaluationRuntime
    display_name: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def consistent_selector(self) -> EvaluationTarget:
        if self.engine_backend not in {EngineBackend.MLX_LM, EngineBackend.MLX_VLM}:
            raise ValueError("evaluation requires a text-capable bare engine")
        validate_transport_target(
            self.target.model_id, self.target.variant_id, self.target, self.public_selector
        )
        return self


class EvaluationAssertion(EvaluationWire):
    kind: Literal["exact", "contains", "json_object", "tool_call", "patch", "retrieval"]
    value: str = Field(default="", max_length=MAX_CASE_BYTES)
    fixture: str | None = Field(default=None, max_length=MAX_CASE_BYTES)


class EvaluationCase(EvaluationWire):
    id: AdapterSlug
    category: Literal[
        "tool_calling",
        "structured_output",
        "edit_application",
        "long_context",
        "coding",
        "instruction",
    ]
    prompt: str = Field(min_length=1, max_length=MAX_CASE_BYTES)
    assertions: list[EvaluationAssertion] = Field(min_length=1, max_length=8)
    reference: str | None = Field(default=None, max_length=MAX_CASE_BYTES)

    @model_validator(mode="after")
    def byte_bounds(self) -> EvaluationCase:
        if any(
            len(text.encode()) > MAX_CASE_BYTES
            for text in [
                self.prompt,
                self.reference or "",
                *(assertion.value for assertion in self.assertions),
                *(assertion.fixture or "" for assertion in self.assertions),
            ]
        ):
            raise ValueError("case exceeds UTF-8 byte bound")
        return self


class EvaluationSuiteTemplate(EvaluationWire):
    template_id: TemplateId
    template_version: int = Field(strict=True, ge=1, le=2**31 - 1)
    kind: SuiteKind
    mode: SuiteMode
    content_sha256: Digest
    cases_sha256: Digest
    scorer_version: Literal["coire-evaluation-v1"] = "coire-evaluation-v1"
    case_count: int = Field(strict=True, ge=1, le=32)
    license: str = Field(min_length=1, max_length=128)


class EvaluationSuiteRegistration(EvaluationWire):
    suite_id: AdapterSlug
    version: int = Field(strict=True, ge=1, le=2**31 - 1)
    template_id: TemplateId
    template_version: Literal[1] = 1
    generation: EvaluationGeneration = Field(default_factory=EvaluationGeneration)
    timeout_seconds: int = Field(default=900, strict=True, ge=60, le=1800)
    judge: EvaluationSubject | None = None
    judge_generation: EvaluationGeneration = Field(default_factory=EvaluationGeneration)

    @model_validator(mode="after")
    def judge_required(self) -> EvaluationSuiteRegistration:
        if self.template_id.startswith("judge-") != (self.judge is not None):
            raise ValueError("judge binding must match the selected suite template")
        return self


class EvaluationSuite(EvaluationWire):
    suite_id: AdapterSlug
    version: int = Field(strict=True, ge=1, le=2**31 - 1)
    registry_version: int = Field(default=1, ge=1, le=2**31 - 1)
    template: EvaluationSuiteTemplate
    generation: EvaluationGeneration
    timeout_seconds: int = Field(ge=60, le=1800)
    judge: EvaluationTarget | None = None
    judge_generation: EvaluationGeneration = Field(default_factory=EvaluationGeneration)
    content_sha256: Digest
    retired: bool = False
    registered_at: AwareDatetime
    registered_by: uuid.UUID | None = None

    @model_validator(mode="after")
    def judge_matches_kind(self) -> EvaluationSuite:
        if (self.template.kind is SuiteKind.JUDGE) != (self.judge is not None):
            raise ValueError("registered suite has an inconsistent judge")
        return self


class EvaluationSubmission(EvaluationWire):
    suite_id: AdapterSlug
    suite_version: int = Field(strict=True, ge=1, le=2**31 - 1)
    subjects: list[EvaluationSubject] = Field(min_length=1, max_length=2)
    training_job_id: TrainingId | None = None
    expected_engine_version: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_subjects(self) -> EvaluationSubmission:
        identities = {(item.model_id, item.variant_id, item.adapter_id) for item in self.subjects}
        if len(identities) != len(self.subjects):
            raise ValueError("evaluation subjects must be distinct")
        return self


class EvaluationControl(EvaluationWire):
    expected_version: int = Field(strict=True, ge=1, le=2**31 - 1)


class EvaluationReceipt(EvaluationWire):
    id: TrainingId
    group_id: TrainingId
    state: EvaluationState
    version: int = Field(ge=1)
    events_path: str = Field(pattern=r"^/api/v1/admin/evaluations/[0-9A-HJKMNP-TV-Z]{26}/events$")


class JudgeRubricOutput(EvaluationWire):
    correctness: int = Field(strict=True, ge=0, le=4)
    instruction_adherence: int = Field(strict=True, ge=0, le=4)
    clarity: int = Field(strict=True, ge=0, le=4)


class JudgePairwiseOutput(EvaluationWire):
    winner: Literal["A", "B", "tie"]


class EvaluationCaseScore(EvaluationWire):
    case_id: AdapterSlug
    subject_index: int = Field(strict=True, ge=0, le=1)
    passed: bool | None = None
    rubric: JudgeRubricOutput | None = None
    score: float | None = Field(default=None, ge=0, le=1)


class EvaluationOutput(EvaluationWire):
    case_id: AdapterSlug
    subject_index: int = Field(strict=True, ge=0, le=1)
    text: str = Field(max_length=MAX_CASE_BYTES)
    prompt_tokens: int = Field(strict=True, ge=0)
    completion_tokens: int = Field(strict=True, ge=0, le=4096)

    @model_validator(mode="after")
    def output_bytes(self) -> EvaluationOutput:
        if len(self.text.encode()) > MAX_CASE_BYTES:
            raise ValueError("output exceeds UTF-8 byte bound")
        return self


class EvaluationPairwiseCase(EvaluationWire):
    case_id: AdapterSlug
    first_order: Literal["AB", "BA"]
    first: JudgePairwiseOutput
    second: JudgePairwiseOutput
    preferred_subject: Literal[0, 1] | None


class EvaluationContamination(EvaluationWire):
    algorithm: Literal["input-exact-v1"] = "input-exact-v1"
    status: Literal["clean", "overlap", "unavailable", "not_applicable"]
    data_sha256: list[Digest] = Field(default_factory=list, max_length=32)
    selected_rows: int = Field(default=0, strict=True, ge=0, le=16_000_000)
    checked_cases: int = Field(default=0, strict=True, ge=0, le=32)
    hit_count: int = Field(default=0, strict=True, ge=0, le=32)
    case_ids: list[AdapterSlug] = Field(default_factory=list, max_length=32)
    reason: (
        Literal["no_training_context", "input_missing", "unsupported_format", "not_task_suite"]
        | None
    ) = None

    @model_validator(mode="after")
    def complete_counts(self) -> EvaluationContamination:
        if (
            self.hit_count > self.checked_cases
            or len(set(self.case_ids)) != len(self.case_ids)
            or len(self.case_ids) != self.hit_count
        ):
            raise ValueError("contamination counts differ from evidence")
        if self.status == "clean" and (
            self.hit_count or not self.checked_cases or not self.selected_rows
        ):
            raise ValueError("clean requires a complete nonempty exact-input check")
        if self.status == "overlap" and not self.hit_count:
            raise ValueError("overlap requires matching case evidence")
        return self


class EvaluationEvidence(EvaluationWire):
    id: uuid.UUID
    sha256: Digest
    bytes: int = Field(strict=True, ge=1, le=MAX_EVALUATION_BYTES)
    expires_at: AwareDatetime
    availability: Literal["present", "expired", "missing"] = "present"


class EvaluationResult(EvaluationWire):
    id: TrainingId
    run_id: TrainingId
    outcome: Literal["succeeded", "failed", "timed_out", "cancelled"]
    reason: EvaluationReason | None = None
    subjects: list[EvaluationTarget] = Field(min_length=1, max_length=2)
    suite: EvaluationSuite
    cases: list[EvaluationCaseScore] = Field(default_factory=list, max_length=64)
    aggregates: list[Annotated[float | None, Field(ge=0, le=1)]] = Field(min_length=1, max_length=2)
    pairwise: list[EvaluationPairwiseCase] = Field(default_factory=list, max_length=32)
    harness_verdict: Literal["passed", "failed"] | None = None
    harness_evaluation_id: uuid.UUID | None = None
    contamination: EvaluationContamination
    started_at: AwareDatetime
    finished_at: AwareDatetime
    result_sha256: Digest

    @model_validator(mode="after")
    def genuine_outcome(self) -> EvaluationResult:
        if len(self.aggregates) != len(self.subjects) or self.finished_at < self.started_at:
            raise ValueError("result identities/timing are inconsistent")
        if self.outcome != "succeeded" and (
            any(value is not None for value in self.aggregates) or self.harness_verdict is not None
        ):
            raise ValueError("failed execution cannot claim measured aggregate or verdict")
        return self


class EvaluationRunDetail(EvaluationReceipt):
    suite: EvaluationSuite
    subjects: list[EvaluationTarget] = Field(min_length=1, max_length=2)
    owner_user_id: uuid.UUID
    phase: EvaluationPhase | None = None
    cleanup_state: Literal["none", "pending", "unresolved", "complete"] = "none"
    reason: EvaluationReason | None = None
    queue_deadline: AwareDatetime
    execution_deadline: AwareDatetime | None = None
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    result: EvaluationResult | None = None
    evidence: list[EvaluationEvidence] = Field(default_factory=list, max_length=8)
    source_run_id: TrainingId | None = None
    training_job_id: TrainingId | None = None
    checkpoint_id: uuid.UUID | None = None
    update: int | None = Field(default=None, ge=0, le=100_000)


class EvaluationRunPage(EvaluationWire):
    items: list[EvaluationRunDetail] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)


class EvaluationSuitePage(EvaluationWire):
    items: list[EvaluationSuite] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)


class EvaluationTemplatePage(EvaluationWire):
    items: list[EvaluationSuiteTemplate] = Field(max_length=32)


class EvaluationComparison(EvaluationWire):
    left_result_id: TrainingId | uuid.UUID
    right_result_id: TrainingId | uuid.UUID
    left_subject: int = Field(ge=0, le=1)
    right_subject: int = Field(ge=0, le=1)
    comparable: bool
    reasons: list[
        Literal[
            "outcome",
            "suite",
            "cases",
            "scorer",
            "decoding",
            "runtime",
            "tokenizer",
            "template",
            "capability",
            "judge",
            "legacy_provenance",
        ]
    ] = Field(default_factory=list, max_length=16)
    left_score: float | None = Field(default=None, ge=0, le=1)
    right_score: float | None = Field(default=None, ge=0, le=1)
    delta: float | None = Field(default=None, ge=-1, le=1)
    pairwise: list[EvaluationPairwiseCase] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def no_misleading_delta(self) -> EvaluationComparison:
        if self.comparable == bool(self.reasons) or (
            not self.comparable and self.delta is not None
        ):
            raise ValueError("comparison requires explicit compatibility and reasons")
        return self


class EvaluationGroupDetail(EvaluationWire):
    id: TrainingId
    origin: Literal["manual", "training_checkpoint", "training_final", "measurement"]
    training_job_id: TrainingId | None = None
    checkpoint_id: uuid.UUID | None = None
    update: int | None = Field(default=None, ge=0, le=100_000)
    runs: list[EvaluationRunDetail] = Field(default_factory=list, max_length=8)
    state: Literal["pending", "running", "succeeded", "failed", "cancelled"]
    reason: EvaluationReason | None = None
    created_at: AwareDatetime


class EvaluationEvent(EvaluationWire):
    sequence: int = Field(strict=True, ge=1, le=2**31 - 1)
    evaluation_id: TrainingId
    kind: Literal["state", "progress", "terminal", "reset"]
    state: EvaluationState
    phase: EvaluationPhase | None = None
    version: int = Field(strict=True, ge=1, le=2**31 - 1)
    created_at: AwareDatetime
    reason: EvaluationReason | None = None
    completed_cases: int = Field(default=0, ge=0, le=64)
    snapshot: EvaluationRunDetail | None = None


class EvaluationReplayPage(EvaluationWire):
    events: list[EvaluationEvent] = Field(default_factory=list, max_length=1000)
    cursor: int = Field(ge=0)
    reset: EvaluationRunDetail | None = None


class EvaluationGroupEvent(EvaluationWire):
    sequence: int = Field(strict=True, ge=1, le=2**31 - 1)
    group_id: TrainingId
    kind: Literal["state", "terminal", "reset"]
    state: Literal["pending", "running", "succeeded", "failed", "cancelled"]
    created_at: AwareDatetime
    run: EvaluationReceipt | None = None
    snapshot: EvaluationGroupDetail | None = None


class EvaluationGroupReplayPage(EvaluationWire):
    events: list[EvaluationGroupEvent] = Field(default_factory=list, max_length=1000)
    cursor: int = Field(ge=0)
    reset: EvaluationGroupDetail | None = None


class EvaluationInputFile(EvaluationWire):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    sha256: Digest
    bytes: int = Field(strict=True, ge=1, le=256 * 1024**2)
    purpose: Literal["previous_outputs", "training_source", "split_manifest", "training_state"]


class EvaluationPressureBinding(EvaluationWire):
    measurement_id: uuid.UUID
    max_iterations: int = Field(default=1000, strict=True, ge=1, le=1000)


class EvaluationPressureStop(EvaluationWire):
    run_id: uuid.UUID
    measurement_id: uuid.UUID
    request_sha256: Digest


class EvaluationWorkload(EvaluationWire):
    kind: Literal["evaluation"] = "evaluation"
    workload_version: Literal[1] = 1
    evaluation_id: TrainingId
    attempt_id: uuid.UUID
    run_id: uuid.UUID
    fence: int = Field(strict=True, ge=1, le=2**31 - 1)
    phase: EvaluationPhase
    suite: EvaluationSuite
    target: EvaluationTarget
    subject_index: int = Field(strict=True, ge=0, le=1)
    deadline: AwareDatetime
    subject_count: int = Field(default=1, strict=True, ge=1, le=2)
    previous_outputs: list[EvaluationOutput] = Field(default_factory=list, max_length=64)
    training: EvaluationTrainingBinding | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    input_files: list[EvaluationInputFile] = Field(default_factory=list, max_length=64)
    pressure: EvaluationPressureBinding | None = None

    @model_validator(mode="after")
    def scope_phase(self) -> EvaluationWorkload:
        if self.phase in {"cleanup", "contamination"}:
            raise ValueError("control and CPU scan phases do not accept inference workloads")
        if self.subject_index >= self.subject_count:
            raise ValueError("phase subject index exceeds declared subjects")
        if len({item.name for item in self.input_files}) != len(self.input_files):
            raise ValueError("phase input names must be unique")
        training_files = {
            item.name: item for item in self.input_files if item.purpose != "previous_outputs"
        }
        if self.training is None and training_files:
            raise ValueError("training files require immutable checkpoint input identity")
        if self.training is not None:
            binding = self.training
            expected = {
                "training-state.json": (
                    binding.state_sha256,
                    binding.state_bytes,
                    "training_state",
                ),
            }
            for source in binding.sources:
                expected[f"source-{source.dataset_id}.jsonl"] = (
                    source.source_sha256,
                    source.source_bytes,
                    "training_source",
                )
                expected[f"split-{source.dataset_id}.json"] = (
                    source.split_sha256,
                    source.split_bytes,
                    "split_manifest",
                )
            if {
                name: (item.sha256, item.bytes, item.purpose)
                for name, item in training_files.items()
            } != expected:
                raise ValueError("training files differ from the immutable input binding")
        if self.phase == "judge" and (
            self.suite.judge is None
            or self.target != self.suite.judge
            or not (
                self.previous_outputs
                or any(item.purpose == "previous_outputs" for item in self.input_files)
            )
        ):
            raise ValueError("judge phase requires its exact suite judge and candidate evidence")
        if self.phase != "judge" and self.previous_outputs:
            raise ValueError("only judge phases accept prior candidate outputs")
        return self


class EvaluationWorkspacePrepare(EvaluationWire):
    workload: EvaluationWorkload
    request_sha256: Digest

    @model_validator(mode="after")
    def bind_digest(self) -> EvaluationWorkspacePrepare:
        if canonical_digest(self.workload) != self.request_sha256:
            raise ValueError("workspace request digest differs from workload")
        if len(self.workload.model_dump_json().encode()) > 256 * 1024:
            raise ValueError("workspace request exceeds metadata bound")
        return self


class EvaluationWorkspaceReceipt(EvaluationWire):
    evaluation_id: TrainingId
    attempt_id: uuid.UUID
    run_id: uuid.UUID
    fence: int = Field(strict=True, ge=1, le=2**31 - 1)
    node: StudioName
    workspace_ref: str = Field(pattern=r"^eval-[0-9a-f-]{36}$")
    output_ref: str = Field(pattern=r"^eval-output-[0-9a-f-]{36}$")
    request_sha256: Digest
    suite_sha256: Digest
    agent_version: str = Field(min_length=1, max_length=64)


class EvaluationWorkerResult(EvaluationWire):
    workload_version: Literal[1] = 1
    evaluation_id: TrainingId
    attempt_id: uuid.UUID
    run_id: uuid.UUID
    fence: int = Field(strict=True, ge=1, le=2**31 - 1)
    phase: EvaluationPhase
    request_sha256: Digest
    suite_sha256: Digest
    cases_sha256: Digest
    runtime: EvaluationRuntime
    target: InferenceTarget
    outcome: Literal["succeeded", "failed", "timed_out", "cancelled"]
    reason: EvaluationReason | None = None
    outputs: list[EvaluationOutput] = Field(default_factory=list, max_length=64)
    scores: list[EvaluationCaseScore] = Field(default_factory=list, max_length=64)
    harness_scores: CategoryScores | None = None
    harness_verdict: EvaluationVerdict | None = None
    pairwise: list[EvaluationPairwiseCase] = Field(default_factory=list, max_length=32)
    contamination: EvaluationContamination | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime

    @model_validator(mode="after")
    def bounded_result(self) -> EvaluationWorkerResult:
        if (
            self.finished_at < self.started_at
            or len(self.model_dump_json().encode()) > MAX_EVALUATION_BYTES
        ):
            raise ValueError("worker result timing/bytes exceed bounds")
        if len({(item.case_id, item.subject_index) for item in self.outputs}) != len(self.outputs):
            raise ValueError("worker outputs contain duplicate cases")
        return self


class EvaluationResident(EvaluationWire):
    instance_id: uuid.UUID
    target: InferenceTarget


class EvaluationMeasurementRequest(EvaluationWire):
    evaluation: EvaluationSubmission
    node: StudioName
    resident_targets: list[EvaluationResident] = Field(min_length=1, max_length=32)
    prompt_set_sha256: Digest
    requests_per_phase: int = Field(default=100, strict=True, ge=100, le=2000)
    concurrency: int = Field(default=1, strict=True, ge=1, le=16)
    arrival_interval_ms: int = Field(default=1000, strict=True, ge=1, le=60_000)
    max_input_tokens: Literal[4000] = 4000
    max_output_tokens: int = Field(default=64, strict=True, ge=1, le=1024)

    @model_validator(mode="after")
    def unique_residents(self) -> EvaluationMeasurementRequest:
        if len({item.instance_id for item in self.resident_targets}) != len(self.resident_targets):
            raise ValueError("resident targets must be unique")
        return self


class EvaluationLatency(EvaluationWire):
    instance_id: uuid.UUID
    baseline_requests: int = Field(ge=0)
    mixed_requests: int = Field(ge=0)
    baseline_p95_seconds: float = Field(ge=0)
    mixed_p95_seconds: float = Field(ge=0)
    gateway_p95_seconds: float = Field(ge=0)
    failures: int = Field(ge=0)


class EvaluationMeasurement(EvaluationWire):
    id: uuid.UUID
    version: int = Field(ge=1)
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    request: EvaluationMeasurementRequest
    targets: list[EvaluationLatency] = Field(default_factory=list, max_length=32)
    swap_growth_bytes: int = Field(default=0, ge=0)
    thermal_ok: bool = False
    report_sha256: Digest | None = None
    profile_sha256: Digest | None = None
    valid_until: AwareDatetime | None = None
    reason: EvaluationReason | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def qualification(self) -> EvaluationMeasurement:
        if self.state == "succeeded":
            expected = {item.instance_id for item in self.request.resident_targets}
            if (
                not self.report_sha256
                or not self.profile_sha256
                or not self.valid_until
                or not self.thermal_ok
                or self.swap_growth_bytes
                or {item.instance_id for item in self.targets} != expected
                or len(self.targets) != len(expected)
            ):
                raise ValueError("qualification requires complete resident and safety evidence")
            if any(
                item.baseline_requests < self.request.requests_per_phase
                or item.mixed_requests < self.request.requests_per_phase
                or item.failures
                or item.baseline_p95_seconds > 1.5
                or item.mixed_p95_seconds > 1.5
                or item.gateway_p95_seconds > 0.02
                for item in self.targets
            ):
                raise ValueError("qualification requires passing complete latency samples")
        return self


class EvaluationMeasurementDetail(EvaluationMeasurement):
    profile_status: Literal["unqualified", "qualified", "expired", "invalidated"]
    profile_invalidated_at: AwareDatetime | None = None
    profile_invalidated_reason: EvaluationReason | None = None


class EvaluationGatewayMessage(EvaluationWire):
    role: Literal["assistant"]
    content: str = Field(max_length=MAX_CASE_BYTES)
    reasoning: str | None = Field(default=None, max_length=MAX_CASE_BYTES)
    reasoning_content: str | None = Field(default=None, max_length=MAX_CASE_BYTES)
    tool_calls: None = None
    tool_call_id: None = None
    name: None = None
    function_call: None = None


class EvaluationGatewayChoice(EvaluationWire):
    index: int = Field(strict=True, ge=0)
    message: EvaluationGatewayMessage
    finish_reason: str | None = None
    logprobs: dict[str, object] | None = None


class EvaluationGatewayPromptTokensDetails(EvaluationWire):
    cached_tokens: int = Field(strict=True, ge=0)


class EvaluationGatewayUsage(EvaluationWire):
    prompt_tokens: int = Field(strict=True, ge=0)
    completion_tokens: int = Field(strict=True, ge=0, le=4096)
    total_tokens: int = Field(strict=True, ge=0)
    prompt_tokens_details: EvaluationGatewayPromptTokensDetails | None = None


class EvaluationGatewayResponse(BaseModel):
    # OpenAI-compatible engine responses include additive metadata.
    model_config = ConfigDict(extra="ignore")
    choices: list[EvaluationGatewayChoice] = Field(min_length=1, max_length=1)
    usage: EvaluationGatewayUsage


class EvaluationWorkspaceCleanup(EvaluationWire):
    run_id: uuid.UUID
    request_sha256: Digest


class EvaluationIdentityRequest(EvaluationWire):
    engine_backend: EngineBackend = Field(
        default=EngineBackend.MLX_LM, exclude_if=lambda value: value is EngineBackend.MLX_LM
    )
    engine_id: uuid.UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    target: InferenceTarget
    variant_slug: str = Field(min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    template_override: str | None = Field(default=None, max_length=64 * 1024)
    capability_profile: CapabilityProfile

    @model_validator(mode="after")
    def supported_engine(self) -> EvaluationIdentityRequest:
        if self.engine_backend not in {EngineBackend.MLX_LM, EngineBackend.MLX_VLM}:
            raise ValueError("evaluation requires a text-capable bare engine")
        return self


class EvaluationProbeTarget(EvaluationWire):
    instance_id: uuid.UUID
    identity: EvaluationIdentityRequest


class EvaluationProbePrepare(EvaluationWire):
    measurement_id: uuid.UUID
    targets: list[EvaluationProbeTarget] = Field(min_length=1, max_length=32)
    prompt_set_sha256: Digest


class EvaluationProbePrompt(EvaluationWire):
    id: str = Field(pattern=r"^probe-[0-9]+$", max_length=32)
    text: str = Field(min_length=1, max_length=MAX_CASE_BYTES)
    tokens_by_instance: dict[uuid.UUID, int]

    @model_validator(mode="after")
    def bounded_counts(self) -> EvaluationProbePrompt:
        if not 1 <= len(self.tokens_by_instance) <= 32 or any(
            not 1 <= value <= 4000 for value in self.tokens_by_instance.values()
        ):
            raise ValueError("measurement requires actual bounded Studio token counts")
        return self


class EvaluationProbePrepared(EvaluationWire):
    measurement_id: uuid.UUID
    prompt_set_sha256: Digest
    prompts: list[EvaluationProbePrompt] = Field(min_length=1, max_length=8)


class EvaluationProbeCompletion(EvaluationWire):
    instance_id: uuid.UUID
    first_token_seconds: float = Field(ge=0)
    gateway_seconds: float = Field(ge=0)
    input_tokens: int = Field(strict=True, ge=1, le=4000)
    output_tokens: int = Field(strict=True, ge=1, le=1024)


class EvaluationProbeSummary(EvaluationWire):
    count: int = Field(strict=True, ge=1, le=2000)
    p95: float = Field(ge=0)
    overhead_p95: float = Field(ge=0)


class EvaluationCapabilities(EvaluationWire):
    node: StudioName
    workload_versions: list[Literal[1]] = Field(default_factory=list, max_length=1)
    agent_image: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}$"
    )
    harness_version: str | None = Field(default=None, min_length=1, max_length=64)


class EvaluationInputReceipt(EvaluationWire):
    run_id: uuid.UUID
    request_sha256: Digest
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    sha256: Digest
    bytes: int = Field(strict=True, ge=1, le=256 * 1024**2)


class EvaluationTrainingInputsReceipt(EvaluationWire):
    items: list[EvaluationInputReceipt] = Field(min_length=2, max_length=17)
