"""Strict wire contracts for durable model acquisition and variant publication."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AcquisitionStage(StrEnum):
    INSPECT = "inspect"
    PULL = "pull"
    CONVERT = "convert"
    VALIDATE = "validate"
    REPLICATE = "replicate"
    DONE = "done"
    FAILED = "failed"


class AcquisitionState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_CAPACITY = "waiting_for_capacity"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class VariantState(StrEnum):
    REQUESTED = "requested"
    INSPECTING = "inspecting"
    QUEUED = "queued"
    PULLING = "pulling"
    CONVERTING = "converting"
    VALIDATING = "validating"
    REPLICATING = "replicating"
    READY = "ready"
    FAILED = "failed"


class Precision(StrEnum):
    BF16 = "bf16"
    FP16 = "fp16"
    BIT4 = "4bit"
    BIT6 = "6bit"
    BIT8 = "8bit"
    MIXED = "mixed"


class QuantizationMode(StrEnum):
    AFFINE = "affine"
    MXFP4 = "mxfp4"
    NVFP4 = "nvfp4"
    MXFP8 = "mxfp8"


class ValidationOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_COMPARABLE = "not_comparable"
    NOT_APPLICABLE = "not_applicable"


class VariantRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    precision: Precision
    bits: int | None = Field(default=None, ge=2, le=8)
    group_size: int | None = Field(default=None)
    mode: QuantizationMode | None = None
    mixed_recipe: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def validate_recipe(self) -> VariantRecipe:
        if self.group_size not in (None, 32, 64, 128):
            raise ValueError("group_size must be one of 32, 64, or 128")
        quantized = self.precision in {
            Precision.BIT4,
            Precision.BIT6,
            Precision.BIT8,
            Precision.MIXED,
        }
        if not quantized and any((self.bits, self.group_size, self.mode, self.mixed_recipe)):
            raise ValueError("bf16/fp16 variants cannot include quantization settings")
        if self.precision is Precision.MIXED and not self.mixed_recipe:
            raise ValueError("mixed precision requires mixed_recipe")
        if self.precision is not Precision.MIXED and self.mixed_recipe:
            raise ValueError("mixed_recipe requires mixed precision")
        return self


class AcquisitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    revision: str | None = Field(default=None, max_length=128)
    keep_raw: bool = False
    variant: VariantRecipe


class VariantPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    published: bool | None = None
    is_default: bool | None = None

    @model_validator(mode="after")
    def at_least_one_change(self) -> VariantPublication:
        if self.published is None and self.is_default is None:
            raise ValueError("published or is_default is required")
        if self.is_default and self.published is False:
            raise ValueError("the default variant must be published")
        return self


class FitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str
    precision: Precision
    required_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    fits: bool


class InspectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str
    architecture: str | None = None
    source_format: str
    gated: bool = False
    metadata_bytes: int = Field(ge=0)
    weight_bytes: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    supported: bool
    rejection_code: str | None = None
    rejection_detail: str | None = None
    source_repo_guidance: str | None = None
    fit: list[FitDecision] = Field(default_factory=list)


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validator_version: str
    smoke: ValidationOutcome
    perplexity: float | None = Field(default=None, ge=0.0)
    reference_variant_id: uuid.UUID | None = None
    reference_perplexity: float | None = Field(default=None, ge=0.0)
    tolerance: float = Field(ge=0.0)
    perplexity_outcome: ValidationOutcome
    template: ValidationOutcome
    validated: bool
    created_at: datetime


class StageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: AcquisitionStage
    status: StageStatus
    attempt: int = Field(ge=0)
    public_summary: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ModelVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    model_id: uuid.UUID
    name: str
    precision: Precision
    recipe: VariantRecipe
    state: VariantState
    byte_size: int = Field(ge=0)
    memory_estimate_bytes: int = Field(ge=0)
    estimate_delta_bytes: int | None = None
    validated: bool
    published: bool
    is_default: bool
    raw_retained: bool
    validation: ValidationResult | None = None
    created_at: datetime
    updated_at: datetime


class AcquisitionWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    model_id: uuid.UUID
    variant_id: uuid.UUID
    stage: AcquisitionStage
    state: AcquisitionState
    progress_bytes: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)
    failure_code: str | None = None
    failure_detail: str | None = None
    stages: list[StageResult] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ReservationState(StrEnum):
    HELD = "held"
    RELEASED = "released"
    EXPIRED = "expired"


class ReservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: uuid.UUID
    workflow_id: uuid.UUID
    variant_id: uuid.UUID
    memory_bytes: int = Field(gt=0)
    disk_bytes: int = Field(gt=0)


class Reservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    state: ReservationState
    memory_bytes: int = Field(gt=0)
    disk_bytes: int = Field(gt=0)
    occupants: list[str] = Field(default_factory=list)
