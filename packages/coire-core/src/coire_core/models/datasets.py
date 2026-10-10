"""Immutable uploaded SFT datasets, analysis identity and deterministic mixture intent."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, JsonValue, field_validator, model_validator

from coire_core.models.conversation import Conversation, ImagePart, bounded_json_object
from coire_core.models.preference import PreferenceAnalysis
from coire_core.models.training_types import Digest, PositiveCount, Seed, TrainingWire


class DatasetFormat(StrEnum):
    PREFERENCE = "preference"
    TEXT = "text"
    PROMPT_COMPLETION = "prompt_completion"
    CONVERSATION = "conversation"


class DatasetState(StrEnum):
    UPLOADING = "uploading"
    VALIDATING = "validating"
    ANALYZING = "analyzing"
    READY = "ready"
    ANALYSIS_FAILED = "analysis_failed"
    FAILED = "failed"
    RETIRED = "retired"
    PURGED = "purged"


class SftTextRow(TrainingWire):
    text: str = Field(min_length=1, max_length=1_000_000)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class SftPromptCompletionRow(TrainingWire):
    prompt: str = Field(min_length=1, max_length=1_000_000)
    completion: str = Field(min_length=1, max_length=1_000_000)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class DatasetToolFunction(TrainingWire):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    arguments: dict[str, JsonValue] | str

    @field_validator("arguments")
    @classmethod
    def inert_arguments(cls, value: dict[str, JsonValue] | str) -> dict[str, JsonValue]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                raise ValueError("tool arguments must be a JSON object") from None
        if not isinstance(value, dict):
            raise ValueError("tool arguments must be an object")
        return bounded_json_object(value)


class DatasetToolCall(TrainingWire):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    type: Literal["function"] = "function"
    function: DatasetToolFunction


class DatasetTextPart(TrainingWire):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=1_000_000)


class DatasetMessage(TrainingWire):
    id: uuid.UUID | None = None
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[DatasetTextPart] | None = None
    tool_calls: list[DatasetToolCall] = Field(default_factory=list, max_length=32)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=128)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class DatasetToolDefinitionFunction(TrainingWire):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    description: str = Field(default="", max_length=4096)
    parameters: dict[str, JsonValue]


class DatasetToolDefinition(TrainingWire):
    type: Literal["function"] = "function"
    function: DatasetToolDefinitionFunction


class SftConversationRow(TrainingWire):
    messages: list[DatasetMessage] = Field(min_length=1, max_length=4096)
    tools: list[DatasetToolDefinition] = Field(default_factory=list, max_length=32)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class TrainingExample(TrainingWire):
    conversation: Conversation
    source_row: PositiveCount
    content_mode: Literal["text", "chat"] = "chat"
    loss_policy: Literal["all_tokens", "final_assistant"] = "final_assistant"

    @model_validator(mode="after")
    def supervised_text_and_tools(self) -> TrainingExample:
        messages = self.conversation.messages
        if not messages or messages[-1].role != "assistant":
            raise ValueError("training needs a final assistant target")
        pending: set[str] = set()
        seen: set[str] = set()
        for message in messages:
            if any(isinstance(part, ImagePart) for part in message.parts):
                raise ValueError("image-bearing training examples are unsupported")
            if message.role == "tool":
                if message.tool_call_id not in pending:
                    raise ValueError("tool response must resolve a preceding call exactly once")
                pending.remove(message.tool_call_id)
            elif pending:
                raise ValueError("tool calls must be resolved before the next conversation turn")
            for call in message.tool_calls:
                if call.id in seen:
                    raise ValueError("tool call IDs must be unique throughout the example")
                seen.add(call.id)
                pending.add(call.id)
        # A final assistant tool call is a supervised target, not an executed call.
        final_ids = {call.id for call in messages[-1].tool_calls}
        if pending - final_ids:
            raise ValueError("training context contains unresolved tool calls")
        if self.content_mode == "text":
            if len(messages) != 1 or messages[0].tool_calls or self.conversation.tools:
                raise ValueError("raw text examples contain exactly one text assistant")
            if self.loss_policy != "all_tokens":
                raise ValueError("raw text uses all-token supervision")
        elif self.loss_policy != "final_assistant":
            raise ValueError("chat examples use final-assistant supervision")
        return self

    def content_sha256(self) -> str:
        """Hash model-visible semantic content, excluding record IDs and provenance."""
        payload = {
            "mode": self.content_mode,
            "loss_policy": self.loss_policy,
            "tools": [tool.model_dump(mode="json") for tool in self.conversation.tools],
            "messages": [
                {
                    "role": message.role,
                    "parts": [part.model_dump(mode="json") for part in message.parts],
                    "tool_calls": [call.model_dump(mode="json") for call in message.tool_calls],
                    "tool_call_id": message.tool_call_id,
                }
                for message in self.conversation.messages
            ],
        }
        encoded = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


class DatasetSource(TrainingWire):
    dataset_id: uuid.UUID
    split: Literal["train"] = "train"
    sample_count: int = Field(strict=True, ge=1, le=1_000_000)
    mixture_proportion: float = Field(gt=0, le=1)


class TokenizedTrainingExample(TrainingWire):
    """Private Studio cache shape; completed tokens and explicit shifted loss-mask identity."""

    source_row: PositiveCount
    content_sha256: Digest
    tokens: list[int] = Field(min_length=2, max_length=8192)
    target_mask: list[bool] = Field(min_length=2, max_length=8192)
    target_start: int = Field(strict=True, ge=1, le=8192)

    @model_validator(mode="after")
    def valid_target_mask(self) -> TokenizedTrainingExample:
        if len(self.tokens) != len(self.target_mask) or self.target_start >= len(self.tokens):
            raise ValueError("tokenized training target lengths are inconsistent")
        if any(type(token) is not int or token < 0 or token >= 2**31 for token in self.tokens):
            raise ValueError("training token IDs must be bounded int32 values")
        if any(self.target_mask[: self.target_start]) or not all(
            self.target_mask[self.target_start :]
        ):
            raise ValueError("training mask must select only the declared target suffix")
        return self


class DatasetMixture(TrainingWire):
    datasets: list[DatasetSource] = Field(min_length=1, max_length=16)
    epoch_samples: int = Field(strict=True, ge=1, le=16_000_000)
    mixture_strategy: Literal["weighted", "sequential"] = "weighted"
    replacement: bool = False
    seed: Seed = 0

    @model_validator(mode="after")
    def proportions_and_sources(self) -> DatasetMixture:
        if len({source.dataset_id for source in self.datasets}) != len(self.datasets):
            raise ValueError("dataset identities must be unique in a mixture")
        if not math.isclose(
            math.fsum(source.mixture_proportion for source in self.datasets),
            1.0,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError("mixture proportions must sum to one")
        if not self.replacement and self.epoch_samples > sum(
            source.sample_count for source in self.datasets
        ):
            raise ValueError("epoch draws exceed the selected source pools without replacement")
        return self


class DatasetValidationSources(TrainingWire):
    dataset_ids: list[uuid.UUID] = Field(min_length=1, max_length=16)
    split: Literal["validation"] = "validation"
    max_batches: int = Field(strict=True, ge=1, le=1000, default=25)
    seed: Seed = 0

    @model_validator(mode="after")
    def unique_sources(self) -> DatasetValidationSources:
        if len(set(self.dataset_ids)) != len(self.dataset_ids):
            raise ValueError("validation source IDs must be unique")
        return self


class SplitManifest(TrainingWire):
    schema_version: Literal[1] = 1
    algorithm: Literal["content-group-v1"] = "content-group-v1"
    dataset_id: uuid.UUID
    source_sha256: Digest
    seed: Seed
    train_rows: list[PositiveCount] = Field(min_length=1, max_length=1_000_000)
    validation_rows: list[PositiveCount] = Field(min_length=1, max_length=1_000_000)
    row_content_sha256: list[Digest] = Field(min_length=2, max_length=1_000_000)

    @model_validator(mode="after")
    def disjoint_complete_groups(self) -> SplitManifest:
        train = set(self.train_rows)
        validation = set(self.validation_rows)
        if len(train) != len(self.train_rows) or len(validation) != len(self.validation_rows):
            raise ValueError("split row indices must be unique")
        if train & validation or train | validation != set(
            range(1, len(self.row_content_sha256) + 1)
        ):
            raise ValueError("split must partition all source rows exactly once")
        train_hashes = {self.row_content_sha256[row - 1] for row in train}
        validation_hashes = {self.row_content_sha256[row - 1] for row in validation}
        if train_hashes & validation_hashes:
            raise ValueError("duplicate content cannot cross split partitions")
        return self


class DatasetProvenance(TrainingWire):
    source: str = Field(min_length=1, max_length=2048)
    license_note: str = Field(min_length=1, max_length=2048)


class DatasetUploadRequest(TrainingWire):
    name: str = Field(min_length=1, max_length=120)
    format: DatasetFormat
    provenance: DatasetProvenance
    analysis_model_id: uuid.UUID
    analysis_variant_id: uuid.UUID
    split_seed: Seed = 0
    validation_fraction: float = Field(default=0.05, gt=0, lt=1)


class DatasetUploadIntent(TrainingWire):
    """Immutable audited upload command identity; no source rows or caller paths."""

    metadata: DatasetUploadRequest
    source_sha256: Digest
    source_bytes: int = Field(strict=True, ge=0, le=256 * 1024**2)


class DatasetAnalysisBinding(TrainingWire):
    dataset_id: uuid.UUID
    model_id: uuid.UUID
    variant_id: uuid.UUID
    base_manifest_sha256: Digest
    source_sha256: Digest
    split_sha256: Digest
    format: DatasetFormat
    model_slug: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    template_override: str | None = Field(default=None, max_length=65536)
    enable_thinking: Literal[False] = False


class DatasetRegistrationCommand(TrainingWire):
    intent: DatasetUploadIntent
    analysis: DatasetAnalysisBinding | None = None
    originating_key_id: uuid.UUID | None = None
    originating_key_version: int | None = Field(default=None, strict=True, ge=1)

    @model_validator(mode="after")
    def paired_originating_key(self) -> DatasetRegistrationCommand:
        if (self.originating_key_id is None) != (self.originating_key_version is None):
            raise ValueError("originating key identity and version must be paired")
        return self


class DatasetCursor(TrainingWire):
    created_at: AwareDatetime
    id: uuid.UUID


class DatasetAnalysisDispatch(TrainingWire):
    analysis_id: uuid.UUID
    node_id: uuid.UUID
    node_name: Literal["coire-edge-a", "coire-edge-b"]
    reservation_id: uuid.UUID
    command_id: uuid.UUID
    deadline: AwareDatetime


class DatasetDiagnostic(TrainingWire):
    row: PositiveCount
    field: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.\[\]-]+$")
    code: Literal[
        "invalid_json",
        "invalid_schema",
        "unsupported_image",
        "invalid_tools",
        "overlength",
        "zero_target",
        "invalid_utf8",
        "row_too_large",
        "template_incompatible",
        "token_identical",
    ]


class DatasetReceipt(TrainingWire):
    dataset_id: uuid.UUID
    state: DatasetState
    version: int = Field(ge=1)


class DatasetDetail(TrainingWire):
    warnings: list[Literal["small_sample"]] = Field(
        default_factory=list, max_length=1, exclude_if=lambda value: not value
    )
    id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    format: DatasetFormat
    state: DatasetState
    provenance: DatasetProvenance
    source_sha256: Digest | None = None
    source_bytes: int = Field(ge=0, le=256 * 1024**2)
    row_count: int = Field(ge=0, le=1_000_000)
    split_seed: Seed
    split_manifest_sha256: Digest | None = None
    analysis_id: uuid.UUID | None = None
    invalid_count: int = Field(default=0, ge=0)
    diagnostics: list[DatasetDiagnostic] = Field(default_factory=list, max_length=100)
    version: int = Field(ge=1)
    created_at: AwareDatetime


class DatasetPage(TrainingWire):
    items: list[DatasetDetail] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)


class DatasetAnalyzeRequest(TrainingWire):
    model_id: uuid.UUID
    variant_id: uuid.UUID


class DatasetAnalysisReceipt(TrainingWire):
    analysis_id: uuid.UUID
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]


class TokenDistribution(TrainingWire):
    minimum: int = Field(ge=0)
    maximum: int = Field(ge=0)
    p50: int = Field(ge=0)
    p95: int = Field(ge=0)
    histogram: list[int] = Field(min_length=1, max_length=64)
    upper_bounds: list[int] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def ordered_counts(self) -> TokenDistribution:
        if not self.minimum <= self.p50 <= self.p95 <= self.maximum:
            raise ValueError("token percentiles must be ordered")
        if len(self.histogram) != len(self.upper_bounds) or any(
            count < 0 for count in self.histogram
        ):
            raise ValueError("histogram bounds/counts must match")
        if (
            self.upper_bounds != sorted(set(self.upper_bounds))
            or self.upper_bounds[-1] < self.maximum
        ):
            raise ValueError("histogram bounds must be increasing and cover the maximum")
        return self


class DatasetAnalysis(TrainingWire):
    preference: PreferenceAnalysis | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    id: uuid.UUID
    dataset_id: uuid.UUID
    model_id: uuid.UUID
    variant_id: uuid.UUID
    tokenizer_sha256: Digest | None = None
    template_sha256: Digest | None = None
    runtime_sha256: Digest | None = None
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    tokens: TokenDistribution | None = None
    role_counts: dict[Literal["system", "user", "assistant", "tool"], int] = Field(
        default_factory=dict
    )
    duplicate_rows: int = Field(default=0, ge=0)
    invalid_count: int = Field(default=0, ge=0)
    row_count: int = Field(default=0, ge=0, le=1_000_000)
    diagnostics: list[DatasetDiagnostic] = Field(default_factory=list, max_length=100)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def complete_success_identity(self) -> DatasetAnalysis:
        if self.state == "succeeded" and (
            self.tokenizer_sha256 is None
            or self.template_sha256 is None
            or self.runtime_sha256 is None
            or self.tokens is None
            or self.invalid_count
        ):
            raise ValueError(
                "successful analysis requires complete identities, statistics and valid rows"
            )
        return self

    def validate_binding(self, binding: DatasetAnalysisBinding) -> None:
        if (
            self.dataset_id != binding.dataset_id
            or self.model_id != binding.model_id
            or self.variant_id != binding.variant_id
        ):
            raise ValueError("dataset analysis differs from its input binding")
        if binding.format is not DatasetFormat.PREFERENCE:
            if self.preference is not None:
                raise ValueError("SFT analysis cannot contain preference statistics")
            return
        if self.state != "succeeded":
            if self.preference is not None:
                raise ValueError("failed preference analysis cannot claim complete statistics")
            return
        preference = self.preference
        if preference is None or (
            preference.dataset_id != self.dataset_id
            or preference.source_sha256 != binding.source_sha256
            or preference.split_sha256 != binding.split_sha256
            or preference.row_count != self.row_count
            or preference.tokenizer_sha256 != self.tokenizer_sha256
            or preference.template_sha256 != self.template_sha256
            or preference.runtime_sha256 != self.runtime_sha256
            or preference.duplicate_rows != self.duplicate_rows
        ):
            raise ValueError("preference analysis requires exact complete paired statistics")


class DatasetDeleteRequest(TrainingWire):
    expected_version: int = Field(strict=True, ge=1)


class DatasetDeletionReceipt(TrainingWire):
    dataset_id: uuid.UUID
    state: Literal["retired", "purged"]
    version: int = Field(ge=1)
