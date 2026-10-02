"""Strict scheduler/node/worker image commands with attempt and fence identity."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from coire_core.models.files import SHA256_PATTERN, ULID_PATTERN
from coire_core.models.images import (
    GENERATION_INPUT_MAX_BYTES,
    RECIPE_INPUT_MAX_BYTES,
    ImageCapabilityProfile,
    ImageClassificationResult,
    ImageRecipe,
    ResolvedImageSpec,
)
from coire_core.models.registry import SLUG_PATTERN, ModelKind

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


class ImageAssetValidateRequest(BaseModel):
    """Reserved, offline Studio validation of an acquired image copy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: uuid.UUID
    model_id: uuid.UUID
    slug: str = Field(pattern=SLUG_PATTERN)
    kind: ModelKind
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    reservation_id: uuid.UUID

    @model_validator(mode="after")
    def image_only(self) -> ImageAssetValidateRequest:
        if self.kind is ModelKind.LANGUAGE_MODEL or self.source_revision == "0" * 40:
            raise ValueError("a pinned image asset is required")
        return self


class ImageAssetValidationResult(BaseModel):
    """Content-free proof returned by the reserved Studio validator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    validated: bool
    kind: ModelKind
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    validator_version: str = "image-v1"
    peak_rss_bytes: int = Field(ge=0)
    peak_physical_bytes: int | None = Field(default=None, ge=0)
    peak_physical_delta_bytes: int | None = Field(default=None, ge=0)
    thumbnail_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    image_capability_profile: ImageCapabilityProfile | None = None

    @model_validator(mode="after")
    def evidence_is_kind_specific(self) -> ImageAssetValidationResult:
        if (
            self.validated
            and self.kind is ModelKind.IMAGE_MODEL
            and (
                self.thumbnail_sha256 is None
                or self.image_capability_profile is None
                or self.peak_physical_bytes is None
                or self.peak_physical_delta_bytes is None
            )
        ):
            raise ValueError(
                "validated image base requires thumbnail, capability and physical evidence"
            )
        if (
            self.kind is ModelKind.IMAGE_MODEL
            and self.peak_physical_bytes is not None
            and self.peak_physical_delta_bytes is not None
            and (
                self.peak_physical_bytes == 0
                or self.peak_physical_delta_bytes > self.peak_physical_bytes
            )
        ):
            raise ValueError("image physical peak evidence is inconsistent")
        if self.kind is not ModelKind.IMAGE_MODEL and self.image_capability_profile is not None:
            raise ValueError("auxiliary validation cannot claim a base capability")
        return self


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


class ImageWorkerProcessConfig(BaseModel):
    """Node-created private launch file; no caller-supplied model path is accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    load: ImageWorkerLoadRequest
    prompt_cache_max_bytes: int = Field(default=256 * 1024**2, ge=0, le=256 * 1024**2)
    classifier_memory_bytes: int = Field(default=1024**3, ge=256 * 1024**2, le=4 * 1024**3)
    store_dir: Path
    scratch_dir: Path
    token_file: Path
    port: int = Field(ge=1, le=65535)

    @model_validator(mode="after")
    def absolute_local_paths(self) -> ImageWorkerProcessConfig:
        paths = (self.store_dir, self.scratch_dir, self.token_file)
        if any(not path.is_absolute() or ".." in path.parts for path in paths):
            raise ValueError("worker launch paths must be absolute and normalized")
        if (
            len(set(paths)) != len(paths)
            or self.token_file.is_relative_to(self.store_dir)
            or self.token_file.is_relative_to(self.scratch_dir)
            or self.scratch_dir.is_relative_to(self.store_dir)
            or self.store_dir.is_relative_to(self.scratch_dir)
        ):
            raise ValueError("worker launch paths cannot overlap")
        return self


class ImageWorkerProcessRecord(BaseModel):
    """Durable node-owned identity; never includes the bearer value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    config: ImageWorkerProcessConfig
    status: ImageWorkerLoadResult

    @model_validator(mode="after")
    def process_identity_complete(self) -> ImageWorkerProcessRecord:
        if (
            self.status.state not in {"starting", "ready"}
            or self.status.instance_id != self.config.load.instance_id
            or self.status.port != self.config.port
            or self.status.reserved_bytes != self.config.load.reservation_bytes
            or self.status.pid is None
            or self.status.process_create_time is None
        ):
            raise ValueError("image worker process record differs from launch")
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


class NodeImageInputReceipt(NodeImageInputRequest):
    staged_at: AwareDatetime


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


class ImageTransferUploadRequest(ImageJobBinding):
    """Bound path and headers for a node's streamed private PNG upload."""

    node: str = Field(pattern=NODE_PATTERN)
    index: int = Field(ge=0, le=3)
    grant_token: str = Field(min_length=8, max_length=512)


class NodeImageTransferRequest(ImageJobBinding):
    """One scheduler command carrying only exact node-bound output grants."""

    node: str = Field(pattern=NODE_PATTERN)
    grants: tuple[ImageTransferGrant, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def grants_match(self) -> NodeImageTransferRequest:
        if any(
            grant.job_id != self.job_id
            or grant.attempt != self.attempt
            or grant.fence != self.fence
            or grant.node != self.node
            for grant in self.grants
        ):
            raise ValueError("transfer grant binding mismatch")
        if len({grant.index for grant in self.grants}) != len(self.grants):
            raise ValueError("transfer grant index duplicated")
        return self


class ImageTransferReceipt(ImageJobBinding):
    node: str = Field(pattern=NODE_PATTERN)
    index: int = Field(ge=0, le=3)
    output_id: uuid.UUID
    byte_count: int = Field(ge=1, le=RECIPE_INPUT_MAX_BYTES)
    sha256: str = Field(pattern=SHA256_PATTERN)
    recipe_sha256: str = Field(pattern=SHA256_PATTERN)
    classification: ImageClassificationResult | None = None
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


class ImageWorkerOutputManifest(BaseModel):
    """Path-free generated output identity for node transfer verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0, le=3)
    byte_count: int = Field(ge=1, le=RECIPE_INPUT_MAX_BYTES)
    sha256: str = Field(pattern=SHA256_PATTERN)
    recipe_sha256: str = Field(pattern=SHA256_PATTERN)
    classification: ImageClassificationResult | None = None


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
    outputs: tuple[ImageWorkerOutputManifest, ...] = Field(default_factory=tuple, max_length=4)
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
        if self.outputs and self.state not in {
            "transferring",
            "cancelling",
            "cancelled",
            "failed",
            "succeeded",
        }:
            raise ValueError("output manifests require transferring state")
        if len({output.index for output in self.outputs}) != len(self.outputs):
            raise ValueError("output indexes cannot repeat")
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
    spec = resolved.spec
    purposes: dict[uuid.UUID, str] = {}
    if spec.init_image_id is not None:
        purposes[spec.init_image_id] = "init"
    if spec.mask_id is not None:
        purposes[spec.mask_id] = "mask"
    if spec.control is not None:
        purposes[spec.control.image_id] = "control"
    required_count = sum(
        item is not None
        for item in (
            spec.init_image_id,
            spec.mask_id,
            spec.control.image_id if spec.control else None,
        )
    )
    if len(purposes) != required_count or set(purposes) != {item.input_id for item in manifests}:
        raise ValueError("inputs differ from requested image bindings")
    for item in manifests:
        if (
            item.purpose != purposes[item.input_id]
            or item.width != spec.width
            or item.height != spec.height
        ):
            raise ValueError("input purpose or dimensions differ from image request")


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
