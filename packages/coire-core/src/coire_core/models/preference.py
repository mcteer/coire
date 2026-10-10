"""Inert preference examples, objective values and reproducible paired data."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from coire_core.models.training_types import Digest, Seed, TrainingId, TrainingWire

PREFERENCE_IMPLEMENTATION = "coire-preference-v1"
MAX_PAIR_BYTES = 256 * 1024


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


class PreferenceMessage(TrainingWire):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=128 * 1024)

    @model_validator(mode="after")
    def nonempty_text(self) -> PreferenceMessage:
        if not self.content.strip() or "\x00" in self.content:
            raise ValueError("preference prompt requires nonempty inert text")
        return self


class PreferenceRow(TrainingWire):
    prompt: list[PreferenceMessage] = Field(min_length=1, max_length=128)
    chosen: str = Field(min_length=1, max_length=65536)
    rejected: str = Field(min_length=1, max_length=65536)

    @model_validator(mode="after")
    def complete_pair(self) -> PreferenceRow:
        messages = self.prompt[1:] if self.prompt[0].role == "system" else self.prompt
        if not messages or messages[-1].role != "user":
            raise ValueError("preference prompt must end with a user message")
        if any(
            message.role != ("user" if index % 2 == 0 else "assistant")
            for index, message in enumerate(messages)
        ):
            raise ValueError("preference prompt must alternate user and assistant")
        if any(
            not answer.strip() or "\x00" in answer or len(answer.encode()) > 65536
            for answer in (self.chosen, self.rejected)
        ):
            raise ValueError("preference responses exceed their bounds or are empty")
        if self.chosen == self.rejected:
            raise ValueError("preference responses must differ")
        if (
            len(canonical_bytes([message.model_dump(mode="json") for message in self.prompt]))
            > 128 * 1024
        ):
            raise ValueError("preference prompt exceeds its byte bound")
        if len(canonical_bytes(self.model_dump(mode="json"))) > MAX_PAIR_BYTES:
            raise ValueError("preference row exceeds its byte bound")
        return self

    def prompt_sha256(self) -> str:
        return hashlib.sha256(
            canonical_bytes([message.model_dump(mode="json") for message in self.prompt])
        ).hexdigest()

    def content_sha256(self) -> str:
        return hashlib.sha256(canonical_bytes(self.model_dump(mode="json"))).hexdigest()


class PreferenceSplitManifest(TrainingWire):
    schema_version: Literal[1] = 1
    algorithm: Literal["coire-preference-split-v1"] = "coire-preference-split-v1"
    dataset_id: uuid.UUID
    source_sha256: Digest
    seed: Seed
    validation_fraction: float = Field(default=0.05, gt=0, lt=1)
    train_rows: list[int] = Field(min_length=1, max_length=1_000_000)
    validation_rows: list[int] = Field(min_length=1, max_length=1_000_000)
    row_content_sha256: list[Digest] = Field(min_length=2, max_length=1_000_000)
    prompt_group_sha256: list[Digest] = Field(min_length=2, max_length=1_000_000)

    @model_validator(mode="after")
    def partition_groups(self) -> PreferenceSplitManifest:
        train, validation = set(self.train_rows), set(self.validation_rows)
        if len(self.prompt_group_sha256) != len(self.row_content_sha256):
            raise ValueError("preference group and row identities must align")
        if (
            len(train) != len(self.train_rows)
            or len(validation) != len(self.validation_rows)
            or train & validation
            or train | validation != set(range(1, len(self.row_content_sha256) + 1))
        ):
            raise ValueError("preference split must partition every row exactly once")
        if {self.prompt_group_sha256[i - 1] for i in train} & {
            self.prompt_group_sha256[i - 1] for i in validation
        }:
            raise ValueError("preference prompt groups cannot cross splits")
        return self


class DpoOptions(TrainingWire):
    beta: float = Field(strict=True, gt=0, le=1)


class OrpoOptions(TrainingWire):
    weight: float = Field(strict=True, ge=0, le=1)


class PreferenceResourceEnvelope(TrainingWire):
    weight_bytes: int = Field(ge=1)
    reference_weight_bytes: int = Field(ge=0)
    adapter_bytes: int = Field(ge=1)
    reference_adapter_bytes: int = Field(ge=0)
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
            + self.reference_weight_bytes
            + self.adapter_bytes
            + self.reference_adapter_bytes
            + self.optimizer_bytes
            + self.activation_bytes
            + self.buffer_bytes
            + self.safety_bytes
        )


class TokenizedPreferenceExample(TrainingWire):
    source_row: int = Field(strict=True, ge=1)
    content_sha256: Digest
    prompt_sha256: Digest
    chosen_tokens: list[int] = Field(min_length=2, max_length=8192)
    rejected_tokens: list[int] = Field(min_length=2, max_length=8192)
    chosen_mask: list[bool] = Field(min_length=2, max_length=8192)
    rejected_mask: list[bool] = Field(min_length=2, max_length=8192)
    prompt_length: int = Field(strict=True, ge=1, le=8191)

    @model_validator(mode="after")
    def response_masks(self) -> TokenizedPreferenceExample:
        for tokens, mask in (
            (self.chosen_tokens, self.chosen_mask),
            (self.rejected_tokens, self.rejected_mask),
        ):
            if (
                len(tokens) != len(mask)
                or self.prompt_length >= len(tokens)
                or any(mask[: self.prompt_length])
                or not all(mask[self.prompt_length :])
            ):
                raise ValueError("paired target masks must select the complete response suffix")
            if any(type(token) is not int or not 0 <= token < 2**31 for token in tokens):
                raise ValueError("preference token ids must be bounded integers")
        if (
            self.chosen_tokens[: self.prompt_length] != self.rejected_tokens[: self.prompt_length]
            or self.chosen_tokens == self.rejected_tokens
        ):
            raise ValueError("paired sequences need one prompt and distinct responses")
        return self


class PreferenceSamplerState(TrainingWire):
    kind: Literal["preference"] = "preference"
    version: Literal["coire-pair-sampler-v1"] = "coire-pair-sampler-v1"
    dataset_sha256: Digest
    seed: Seed
    epoch: int = Field(strict=True, ge=0)
    cursor: int = Field(strict=True, ge=0)
    batch_size: int = Field(strict=True, ge=1, le=64)


class PreferenceProbe(TrainingWire):
    scope: Literal["held_out_post_update"] = "held_out_post_update"
    sample_count: int = Field(strict=True, ge=1, le=8)
    accuracy: float = Field(ge=0, le=1)
    margin: float
    chosen_nll: float | None = None
    odds_penalty: float | None = None


class PreferenceMetricSample(TrainingWire):
    job_id: TrainingId
    attempt_id: TrainingId
    fence: int = Field(strict=True, ge=1)
    update: int = Field(strict=True, ge=0, le=100_000)
    objective: Literal["dpo", "orpo"]
    implementation: Literal["coire-preference-v1"] = "coire-preference-v1"
    normalization: Literal["pair_mean"] = "pair_mean"
    kind: Literal["train", "validation"]
    loss: float
    pair_count: int = Field(strict=True, ge=1, le=4096)
    response_tokens: int = Field(strict=True, ge=0, le=67_108_864)
    tokens_per_second: float = Field(ge=0)
    probe: PreferenceProbe | None = None
    learning_rate: float = Field(default=0, ge=0)
    updates_per_second: float = Field(default=0, ge=0)
    footprint_bytes: int = Field(default=0, ge=0)
    peak_bytes: int = Field(default=0, ge=0)
    rolled_back: bool = False
    recorded_at: AwareDatetime


class PreferenceBatch(TrainingWire):
    """Padded chosen/rejected rows; prompt/padding targets remain false."""

    chosen_tokens: list[list[int]] = Field(min_length=1, max_length=64)
    rejected_tokens: list[list[int]] = Field(min_length=1, max_length=64)
    chosen_masks: list[list[bool]] = Field(min_length=1, max_length=64)
    rejected_masks: list[list[bool]] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def aligned_pairs(self) -> PreferenceBatch:
        size = len(self.chosen_tokens)
        if any(
            len(rows) != size
            for rows in (self.rejected_tokens, self.chosen_masks, self.rejected_masks)
        ):
            raise ValueError("preference batches require aligned complete pairs")
        widths = {len(row) for row in self.chosen_tokens + self.rejected_tokens}
        if len(widths) != 1 or not 2 <= next(iter(widths)) <= 8192:
            raise ValueError("preference batches must be rectangular and bounded")
        for tokens, masks in (
            (self.chosen_tokens, self.chosen_masks),
            (self.rejected_tokens, self.rejected_masks),
        ):
            for row, mask in zip(tokens, masks, strict=True):
                if len(row) != len(mask) or mask[0] or not any(mask[1:]):
                    raise ValueError("every response needs aligned shifted targets")
                if any(type(token) is not int or not 0 <= token < 2**31 for token in row):
                    raise ValueError("preference token ids must be bounded int32")
        return self


class PreferenceTokenSummary(TrainingWire):
    minimum: int = Field(strict=True, ge=1, le=8192)
    maximum: int = Field(strict=True, ge=1, le=8192)
    total: int = Field(strict=True, ge=1, le=8192_000_000)

    @model_validator(mode="after")
    def ordered_bounds(self) -> PreferenceTokenSummary:
        if self.minimum > self.maximum:
            raise ValueError("preference token summary bounds are reversed")
        return self


class PreferenceAnalysis(TrainingWire):
    schema_version: Literal[1] = 1
    implementation: Literal["coire-preference-data-v1"] = "coire-preference-data-v1"
    dataset_id: uuid.UUID
    source_sha256: Digest
    split_sha256: Digest
    tokenizer_sha256: Digest
    template_sha256: Digest
    runtime_sha256: Digest
    row_count: int = Field(strict=True, ge=2, le=1_000_000)
    prompt_group_count: int = Field(strict=True, ge=2, le=1_000_000)
    token_rows_sha256: Digest
    chosen_tokens: PreferenceTokenSummary
    rejected_tokens: PreferenceTokenSummary
    chosen_response_tokens: PreferenceTokenSummary
    rejected_response_tokens: PreferenceTokenSummary
    duplicate_rows: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def complete_analysis(self) -> PreferenceAnalysis:
        if self.prompt_group_count > self.row_count:
            raise ValueError("preference prompt groups exceed rows")
        for summary in (
            self.chosen_tokens,
            self.rejected_tokens,
            self.chosen_response_tokens,
            self.rejected_response_tokens,
        ):
            if (
                not summary.minimum * self.row_count
                <= summary.total
                <= summary.maximum * self.row_count
            ):
                raise ValueError("preference token summary does not cover every row")
        if (
            self.chosen_response_tokens.total >= self.chosen_tokens.total
            or self.rejected_response_tokens.total >= self.rejected_tokens.total
        ):
            raise ValueError("preference analysis needs nonempty prompt tokens")
        return self
