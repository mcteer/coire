"""Strict scheduler/node/worker image commands with attempt and fence identity."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from coire_core.models.files import SHA256_PATTERN, ULID_PATTERN
from coire_core.models.images import (
    GENERATION_INPUT_MAX_BYTES,
    RECIPE_INPUT_MAX_BYTES,
    ImageRecipe,
    ResolvedImageSpec,
)
from coire_core.models.registry import SLUG_PATTERN

NODE_PATTERN = r"^coire-[a-z0-9-]{1,50}$"


class ImageRecipeParseRequest(BaseModel):
    """Private file-worker command; no caller-controlled path is accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    input_id: uuid.UUID
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    byte_count: int = Field(ge=1, le=RECIPE_INPUT_MAX_BYTES)


class ImageRecipeParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    input_id: uuid.UUID
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    byte_count: int = Field(ge=1, le=RECIPE_INPUT_MAX_BYTES)
    recipe: ImageRecipe


class ImageJobBinding(BaseModel):
    """Every mutable command identifies one job execution attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    job_id: str = Field(pattern=ULID_PATTERN)
    attempt: int = Field(ge=1)
    fence: int = Field(ge=1)


class ImageWorkerLoadRequest(BaseModel):
    """Registry identity only: coire-node resolves all local component paths."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    backend: Literal["mflux"] = "mflux"
    slug: str = Field(pattern=SLUG_PATTERN)
    model_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    instance_id: uuid.UUID
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    reservation_bytes: int = Field(ge=1)
    runtime_version: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    idle_ttl_seconds: int = Field(default=900, ge=60, le=86_400)

    @field_validator("slug")
    @classmethod
    def exact_slug(cls, value: str) -> str:
        if re.fullmatch(SLUG_PATTERN, value) is None:
            raise ValueError("invalid image model slug")
        return value


class ImageWorkerLoadResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: uuid.UUID
    backend: Literal["mflux"] = "mflux"
    state: Literal["starting", "ready", "failed"]
    pid: int | None = Field(default=None, ge=1)
    process_create_time: float | None = Field(default=None, gt=0)
    port: int | None = Field(default=None, ge=1, le=65535)
    reserved_bytes: int = Field(ge=0)
    safe_error: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def process_identity_complete(self) -> ImageWorkerLoadResult:
        values = (self.pid, self.process_create_time, self.port)
        if self.state == "ready" and any(value is None for value in values):
            raise ValueError("ready worker requires pid, create time and port")
        return self


class NodeImageInputManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_id: uuid.UUID
    purpose: Literal["init", "mask", "control"]
    sha256: str = Field(pattern=SHA256_PATTERN)
    byte_count: int = Field(ge=1, le=GENERATION_INPUT_MAX_BYTES)
    width: int = Field(ge=1, le=4096)
    height: int = Field(ge=1, le=4096)


class NodeImageStartRequest(ImageJobBinding):
    node: str = Field(pattern=NODE_PATTERN)
    model_id: uuid.UUID
    instance_id: uuid.UUID
    resolved: ResolvedImageSpec
    inputs: tuple[NodeImageInputManifest, ...] = Field(default_factory=tuple, max_length=3)
    deadline_at: AwareDatetime
    reservation_bytes: int = Field(ge=1)

    @model_validator(mode="after")
    def model_and_inputs_match(self) -> NodeImageStartRequest:
        if self.model_id != self.resolved.spec.model_id:
            raise ValueError("model_id differs from resolved spec")
        _check_inputs(self.inputs, self.resolved)
        return self


class NodeImageInputRequest(ImageJobBinding):
    node: str = Field(pattern=NODE_PATTERN)
    input_id: uuid.UUID
    purpose: Literal["init", "mask", "control"]
    sha256: str = Field(pattern=SHA256_PATTERN)
    byte_count: int = Field(ge=1, le=GENERATION_INPUT_MAX_BYTES)


class ImageTransferGrantRequest(ImageJobBinding):
    node: str = Field(pattern=NODE_PATTERN)
    index: int = Field(ge=0, le=3)
    expected_bytes: int = Field(ge=1, le=RECIPE_INPUT_MAX_BYTES)
    expected_sha256: str = Field(pattern=SHA256_PATTERN)


class ImageTransferGrant(ImageTransferGrantRequest):
    token: str = Field(min_length=8, max_length=512)
    issued_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def short_lived(self) -> ImageTransferGrant:
        if not self.issued_at < self.expires_at <= self.issued_at + timedelta(minutes=5):
            raise ValueError("expires_at must be within five minutes of issue")
        return self


class ImageTransferReceipt(ImageJobBinding):
    node: str = Field(pattern=NODE_PATTERN)
    index: int = Field(ge=0, le=3)
    output_id: uuid.UUID
    byte_count: int = Field(ge=1, le=RECIPE_INPUT_MAX_BYTES)
    sha256: str = Field(pattern=SHA256_PATTERN)
    recipe_sha256: str = Field(pattern=SHA256_PATTERN)
    verified_at: AwareDatetime


class NodeImageCleanupRequest(ImageJobBinding):
    node: str = Field(pattern=NODE_PATTERN)
    receipts: tuple[ImageTransferReceipt, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def receipts_match(self) -> NodeImageCleanupRequest:
        if any(
            receipt.job_id != self.job_id
            or receipt.attempt != self.attempt
            or receipt.fence != self.fence
            or receipt.node != self.node
            for receipt in self.receipts
        ):
            raise ValueError("receipt job, attempt or fence mismatch")
        if len({receipt.index for receipt in self.receipts}) != len(self.receipts):
            raise ValueError("receipt index duplicated")
        if len({receipt.output_id for receipt in self.receipts}) != len(self.receipts):
            raise ValueError("receipt output_id duplicated")
        return self


class NodeImageCleanupReceipt(ImageJobBinding):
    state: Literal["cleaned"] = "cleaned"
    cleaned_at: AwareDatetime


class NodeImageCancelRequest(ImageJobBinding):
    reason: Literal["user", "admin", "revoked", "deadline", "shutdown"]
    requested_at: AwareDatetime


class NodeImageJob(ImageJobBinding):
    node: str = Field(pattern=NODE_PATTERN)
    instance_id: uuid.UUID
    state: Literal[
        "queued",
        "reserving",
        "running",
        "transferring",
        "cancelling",
        "cancelled",
        "failed",
        "succeeded",
    ]
    pid: int | None = Field(default=None, ge=1)
    process_create_time: float | None = Field(default=None, gt=0)
    progress_step: int | None = Field(default=None, ge=0)
    progress_total: int | None = Field(default=None, ge=1)
    receipts: tuple[ImageTransferReceipt, ...] = Field(default_factory=tuple, max_length=4)
    scratch_cleaned: bool = False
    safe_error: str | None = Field(default=None, max_length=200)
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def receipts_match(self) -> NodeImageJob:
        if any(
            receipt.job_id != self.job_id
            or receipt.attempt != self.attempt
            or receipt.fence != self.fence
            or receipt.node != self.node
            for receipt in self.receipts
        ):
            raise ValueError("receipt binding differs from node job")
        if self.state == "succeeded" and (not self.scratch_cleaned or not self.receipts):
            raise ValueError("succeeded requires receipts and scratch cleanup")
        return self


class ImageWorkerRunRequest(ImageJobBinding):
    instance_id: uuid.UUID
    resolved: ResolvedImageSpec
    inputs: tuple[NodeImageInputManifest, ...] = Field(default_factory=tuple, max_length=3)
    deadline_at: AwareDatetime

    @model_validator(mode="after")
    def inputs_match(self) -> ImageWorkerRunRequest:
        _check_inputs(self.inputs, self.resolved)
        return self


def _check_inputs(
    manifests: tuple[NodeImageInputManifest, ...], resolved: ResolvedImageSpec
) -> None:
    if len({item.input_id for item in manifests}) != len(manifests):
        raise ValueError("inputs cannot repeat input_id")
    actual = {(item.input_id, item.sha256, item.width, item.height) for item in manifests}
    expected = {(item.input_id, item.sha256, item.width, item.height) for item in resolved.inputs}
    if actual != expected:
        raise ValueError("inputs differ from resolved input manifest")


class ImageWorkerOutputManifest(BaseModel):
    """Path-free generated output identity for node transfer verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0, le=3)
    byte_count: int = Field(ge=1, le=RECIPE_INPUT_MAX_BYTES)
    sha256: str = Field(pattern=SHA256_PATTERN)
    recipe_sha256: str = Field(pattern=SHA256_PATTERN)


class ImageWorkerStatus(ImageJobBinding):
    state: Literal["waiting", "running", "generated", "cancelled", "failed"]
    stage: str | None = Field(default=None, max_length=80)
    output_index: int | None = Field(default=None, ge=0, le=3)
    step: int | None = Field(default=None, ge=0)
    total_steps: int | None = Field(default=None, ge=1)
    safe_error: str | None = Field(default=None, max_length=200)
    outputs: tuple[ImageWorkerOutputManifest, ...] = Field(default_factory=tuple, max_length=4)
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def outputs_match_state(self) -> ImageWorkerStatus:
        if self.state == "generated" and not self.outputs:
            raise ValueError("generated worker status requires output manifests")
        if self.state != "generated" and self.outputs:
            raise ValueError("output manifests require generated state")
        if len({output.index for output in self.outputs}) != len(self.outputs):
            raise ValueError("output indexes cannot repeat")
        return self


class ImageWorkerCancelRequest(ImageJobBinding):
    requested_at: AwareDatetime


class ImageWorkerUnloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: uuid.UUID
    reason: Literal["idle_ttl", "admin", "replacement", "shutdown"]
    requested_at: AwareDatetime
