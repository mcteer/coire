"""Authenticated feedback value shapes with no caller-supplied source content."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from coire_core.models.adapters import InferenceTarget
from coire_core.models.gateway import TextCompletionUsage, UsageOutcome
from coire_core.models.preference import PreferenceMessage
from coire_core.models.training_types import Seed, TrainingId, TrainingWire

DISCLOSURE_VERSION: Literal["feedback-v1"] = "feedback-v1"
DISCLOSURE = "Feedback and comparisons may improve models on this platform. Turning capture off or deleting a conversation removes unexported feedback. Already published training datasets and trained adapters remain unchanged."
type FeedbackTag = Annotated[
    str, Field(min_length=1, max_length=32, pattern=r"^[a-z0-9][a-z0-9-]*$")
]
type PairChoice = Literal["original", "candidate"]
type ComparisonState = Literal[
    "queued", "running", "ready", "failed", "cancelled", "identical", "expired", "withdrawn"
]
type SelectionState = Literal["pending", "chosen", "dismissed", "expired", "withdrawn"]
type ExportState = Literal["queued", "staging", "publishing", "succeeded", "failed", "cancelled"]
type FeedbackEligibilityReason = Literal[
    "eligible",
    "capture_disabled",
    "withdrawn",
    "source_provenance_unavailable",
    "unsupported_content",
    "source_not_latest",
    "source_incomplete",
    "source_too_large",
    "expired",
]


class TaggedFeedback(TrainingWire):
    tags: list[FeedbackTag] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def unique_tags(self) -> TaggedFeedback:
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("feedback tags must be unique")
        return self


class FeedbackPreference(TrainingWire):
    owner_id: uuid.UUID
    enabled: bool
    capture_generation: int = Field(ge=1)
    version: int = Field(ge=1)
    disclosure_version: Literal["feedback-v1"] = DISCLOSURE_VERSION
    disclosure: str = DISCLOSURE
    changed_at: AwareDatetime
    withdrawn_at: AwareDatetime | None = None


class FeedbackPreferenceUpdate(TrainingWire):
    client_request_id: uuid.UUID
    expected_version: int = Field(strict=True, ge=1)
    enabled: bool
    disclosure_version: Literal["feedback-v1"]


class ThumbUpdate(TaggedFeedback):
    client_request_id: uuid.UUID
    expected_version: int = Field(strict=True, ge=0)
    judgement: Literal["up", "down"] | None


class FeedbackReceipt(TrainingWire):
    id: uuid.UUID
    version: int = Field(ge=1)
    judgement: Literal["up", "down", "original", "candidate"] | None
    source: Literal["owner", "admin"]
    disclosure_version: Literal["feedback-v1"] = DISCLOSURE_VERSION
    disclosure: str = DISCLOSURE


class MessageFeedback(TaggedFeedback):
    message_id: uuid.UUID
    feedback: FeedbackReceipt | None = None
    eligibility: FeedbackEligibilityReason


class ConversationFeedbackPage(TrainingWire):
    items: list[MessageFeedback] = Field(max_length=100)
    comparisons: list[ComparisonReceipt] = Field(default_factory=list, max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)


class ComparisonCreate(TrainingWire):
    client_request_id: uuid.UUID
    expected_revision: int = Field(strict=True, ge=1)
    source_message_id: uuid.UUID


class ComparisonSelect(TaggedFeedback):
    client_request_id: uuid.UUID
    expected_version: int = Field(strict=True, ge=1)
    candidate: PairChoice


class ComparisonDismiss(TrainingWire):
    client_request_id: uuid.UUID
    expected_version: int = Field(strict=True, ge=1)


class ComparisonReceipt(TrainingWire):
    id: TrainingId
    version: int = Field(ge=1)
    state: ComparisonState
    selection_state: SelectionState
    events_path: str = Field(pattern=r"^/api/v1/chat/conversations/[0-9a-f-]{36}/events$")


class ComparisonAccounting(TrainingWire):
    """Durable content-free settlement identity, independent of contribution bodies."""

    request_id: uuid.UUID
    owner_id: uuid.UUID
    principal_kind: Literal["user", "admin", "api_key"]
    principal_subject: str | None = Field(default=None, max_length=256)
    api_key_id: uuid.UUID | None = None
    model_id: uuid.UUID
    variant_id: uuid.UUID
    adapter_id: uuid.UUID | None = None
    engine_id: uuid.UUID | None = None
    instance_id: uuid.UUID | None = None
    prompt_tokens: int = Field(default=0, strict=True, ge=0, le=2147483647)
    completion_tokens: int = Field(default=0, strict=True, ge=0, le=2147483647)
    started_at: AwareDatetime
    first_token_at: AwareDatetime | None = None
    first_token_duration_ms: float | None = Field(default=None, ge=0)
    outcome: UsageOutcome = UsageOutcome.FAILED
    settled: bool = False


class ComparisonDetail(ComparisonReceipt):
    conversation_id: uuid.UUID
    source_message_id: uuid.UUID
    target: InferenceTarget | None
    original: str | None = Field(default=None, max_length=65536)
    candidate: str | None = Field(default=None, max_length=65536)
    selected: PairChoice | None = None
    usage: TextCompletionUsage | None = None
    owner_judgement: FeedbackReceipt | None = None
    admin_judgement: FeedbackReceipt | None = None
    owner_tags: list[FeedbackTag] = Field(default_factory=list, max_length=16)
    admin_tags: list[FeedbackTag] = Field(default_factory=list, max_length=16)
    eligibility: FeedbackEligibilityReason
    expires_at: AwareDatetime
    created_at: AwareDatetime
    disclosure_version: Literal["feedback-v1"] = DISCLOSURE_VERSION
    disclosure: str = DISCLOSURE


class ComparisonSelectionReceipt(TrainingWire):
    comparison: ComparisonReceipt
    feedback: FeedbackReceipt
    conversation_revision: int = Field(ge=1)


class AdminPairJudgement(TaggedFeedback):
    expected_version: int = Field(strict=True, ge=0)
    choice: Literal["original", "candidate", "skip"]


class FeedbackReviewDetail(ComparisonDetail):
    owner_id: uuid.UUID
    prompt: list[PreferenceMessage] = Field(default_factory=list, max_length=128)


class FeedbackReviewPage(TrainingWire):
    items: list[FeedbackReviewDetail] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)


class FeedbackReviewReceipt(TrainingWire):
    comparison_id: TrainingId
    judgement: FeedbackReceipt | None = None
    skipped: bool = False


class PreferenceExportFilters(TrainingWire):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)
    model_id: uuid.UUID | None = None
    variant_id: uuid.UUID | None = None
    adapter_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    tag: FeedbackTag | None = None
    from_time: AwareDatetime | None = Field(default=None, alias="from")
    until: AwareDatetime | None = None

    @model_validator(mode="after")
    def ordered_range(self) -> PreferenceExportFilters:
        if self.from_time is not None and self.until is not None and self.from_time >= self.until:
            raise ValueError("feedback date range must be increasing")
        if (self.variant_id is not None or self.adapter_id is not None) and self.model_id is None:
            raise ValueError("variant or adapter filters require their parent model")
        return self


class PreferenceExportCreate(TrainingWire):
    name: str = Field(min_length=1, max_length=120)
    license_note: str = Field(min_length=1, max_length=2048)
    model_id: uuid.UUID
    variant_id: uuid.UUID
    split_seed: Seed = 0
    validation_fraction: float = Field(default=0.05, gt=0, lt=1)
    filters: PreferenceExportFilters = Field(default_factory=PreferenceExportFilters)
    source: Literal["owner_preferred", "owner", "admin"] = "owner_preferred"


class PreferenceExportCancel(TrainingWire):
    expected_version: int = Field(strict=True, ge=1)


class PreferenceExportReceipt(TrainingWire):
    id: TrainingId
    state: ExportState
    version: int = Field(ge=1)


class PreferenceExportDetail(PreferenceExportReceipt):
    owner_id: uuid.UUID
    request: PreferenceExportCreate
    selected_count: int = Field(ge=0, le=10000)
    matched_count: int = Field(default=0, ge=0)
    pair_limit: Literal[10000] = 10000
    byte_limit: Literal[268435456] = 268435456
    excluded_count: int = Field(ge=0)
    warnings: list[Literal["small_sample"]] = Field(default_factory=list, max_length=1)
    dataset_id: uuid.UUID | None = None
    cleanup_pending: bool
    reason: (
        Literal[
            "empty",
            "oversize",
            "insufficient_groups",
            "source_changed",
            "unauthorized",
            "quota",
            "timeout",
            "cancelled",
            "invalid_data",
            "internal",
        ]
        | None
    ) = None
    created_at: AwareDatetime
    deadline: AwareDatetime


class PreferenceExportPage(TrainingWire):
    items: list[PreferenceExportDetail] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)


class AdapterLineageEntry(TrainingWire):
    adapter_id: uuid.UUID
    target: InferenceTarget
    objective: Literal["sft", "dpo", "orpo"]
    source_job_id: TrainingId
    dataset_ids: list[uuid.UUID] = Field(max_length=32)
    resolved_spec_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: AwareDatetime


class AdapterLineage(TrainingWire):
    adapter_id: uuid.UUID
    base: InferenceTarget
    parent: InferenceTarget | None
    ancestors: list[AdapterLineageEntry] = Field(max_length=32)


class ComparisonEvent(TrainingWire):
    type: Literal[
        "comparison.accepted",
        "comparison.status",
        "comparison.delta",
        "comparison.ready",
        "comparison.selection",
        "comparison.terminal",
    ]
    comparison_id: TrainingId
    version: int = Field(ge=1)
    state: ComparisonState
    selection_state: SelectionState
    text_delta: str | None = Field(default=None, max_length=65536)
    text_offset: int = Field(default=0, ge=0, le=65536)
    text_length: int = Field(default=0, ge=0, le=65536)


ConversationFeedbackPage.model_rebuild()


class FeedbackGenerationSettings(TrainingWire):
    temperature: float = Field(ge=0, le=2)
    top_p: float = Field(gt=0, le=1)
    max_tokens: int = Field(strict=True, ge=1, le=8192)
    seed: Seed | None
    top_k: int = Field(default=0, strict=True, ge=0)
    min_p: float = Field(default=0.0, ge=0, le=1)
    enable_thinking: bool = False


class ChatFeedbackProvenance(TrainingWire):
    source_turn_id: uuid.UUID
    source_message_id: uuid.UUID
    conversation_id: uuid.UUID
    owner_id: uuid.UUID
    capture_generation: int = Field(strict=True, ge=1)
    context_revision: int = Field(strict=True, ge=1)
    target: InferenceTarget
    tokenizer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt: list[PreferenceMessage] = Field(min_length=1, max_length=128)
    settings: FeedbackGenerationSettings
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def bounded_snapshot(self) -> ChatFeedbackProvenance:
        from coire_core.models.preference import PreferenceRow

        PreferenceRow(prompt=self.prompt, chosen="original", rejected="candidate")
        if self.expires_at <= self.created_at:
            raise ValueError("provenance expiry must follow capture")
        return self
