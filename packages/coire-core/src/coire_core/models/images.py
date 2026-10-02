"""Version 1 image request, asset, event and reproducible recipe contracts."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from coire_core.models.files import SHA256_PATTERN, SLUG_PATTERN, ULID_PATTERN

UINT32_MAX = 2**32 - 1
IMAGE_RUNTIME_VERSION = "mflux-0.20.0"
GENERATION_INPUT_MAX_BYTES = 10 * 1024 * 1024
RECIPE_INPUT_MAX_BYTES = 64 * 1024 * 1024
RECIPE_METADATA_MAX_BYTES = 64 * 1024


class ImageMode(StrEnum):
    TXT2IMG = "txt2img"
    IMG2IMG = "img2img"
    FILL = "fill"
    CONTROL = "control"


class ImageContentMode(StrEnum):
    STANDARD = "standard"
    EXPLICIT = "explicit"


class ImageContentTag(StrEnum):
    NORMAL = "normal"
    EXPLICIT = "explicit"
    UNKNOWN = "unknown"


type ImageClassifierDiagnostic = Literal[
    "classifier_unavailable",
    "classifier_failed",
    "classifier_timeout",
    "classifier_memory",
    "classifier_invalid_result",
]


class ImageClassificationResult(BaseModel):
    """Bounded Studio classifier IPC and persisted gallery-tag provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: ImageContentTag
    score: Decimal | None = Field(default=None, ge=0, le=1)
    threshold: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)
    classifier_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    processor_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    safe_error: (
        Literal[
            "classifier_failed",
            "classifier_timeout",
            "classifier_memory",
            "classifier_invalid_result",
        ]
        | None
    ) = None
    tagged_at: AwareDatetime

    @model_validator(mode="after")
    def consistent_result(self) -> ImageClassificationResult:
        if self.threshold != Decimal("0.5"):
            raise ValueError("classifier threshold must be 0.5")
        if self.tag is ImageContentTag.UNKNOWN:
            if self.score is not None or self.safe_error is None:
                raise ValueError("unknown classification requires a safe diagnostic")
        elif self.tag is ImageContentTag.NORMAL and self.score is None:
            raise ValueError("normal classification requires a score")
        elif self.score is None and self.safe_error is None:
            raise ValueError("known classification requires a score or safe diagnostic")
        if self.score is not None and (
            self.processor_sha256 is None or self.safe_error is not None
        ):
            raise ValueError("scored classification requires processor digest and no error")
        return self


class ImageJobState(StrEnum):
    QUEUED = "queued"
    RESERVING = "reserving"
    RUNNING = "running"
    TRANSFERRING = "transferring"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ImageLora(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    scale: Decimal = Field(ge=Decimal("-2"), le=Decimal("2"))

    @field_validator("scale")
    @classmethod
    def finite_scale(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("scale must be finite")
        return value


class ImageControl(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["canny"] = "canny"
    image_id: uuid.UUID
    model_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    strength: Decimal = Field(default=Decimal("1"), gt=0, le=1)
    low_threshold: int = Field(default=100, ge=0, le=255)
    high_threshold: int = Field(default=200, ge=0, le=255)

    @model_validator(mode="after")
    def thresholds_ordered(self) -> ImageControl:
        if self.low_threshold >= self.high_threshold:
            raise ValueError("low_threshold must be below high_threshold")
        return self


class ImageUpscale(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    factor: Literal[2, 4]


class ImageOutputSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["png"] = "png"
    embed_metadata: Literal[True] = True


class ImageSpec(BaseModel):
    """Fully specified client-side values before model-capability validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    model_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    mode: ImageMode = ImageMode.TXT2IMG
    prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str | None = Field(default=None, max_length=4000)
    width: int = Field(ge=64, le=4096, multiple_of=8)
    height: int = Field(ge=64, le=4096, multiple_of=8)
    steps: int = Field(ge=1, le=150)
    guidance: Decimal = Field(ge=0, le=30)
    seed: int | None = Field(default=None, ge=0, le=UINT32_MAX)
    n: int = Field(default=1, ge=1, le=4)
    loras: tuple[ImageLora, ...] = Field(default_factory=tuple, max_length=4)
    init_image_id: uuid.UUID | None = None
    strength: Decimal | None = Field(default=None, gt=0, le=1)
    mask_id: uuid.UUID | None = None
    control: ImageControl | None = None
    upscale: ImageUpscale | None = None
    output: ImageOutputSettings = Field(default_factory=ImageOutputSettings)
    content_mode: ImageContentMode = ImageContentMode.STANDARD

    @field_validator("guidance", "strength")
    @classmethod
    def finite_decimal(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("numeric value must be finite")
        return value

    @model_validator(mode="after")
    def fixed_pipeline(self) -> ImageSpec:
        if self.width * self.height > 16_000_000:
            raise ValueError("width and height exceed the 16 MP bound")
        if self.upscale is not None and (
            self.width * self.upscale.factor > 4096
            or self.height * self.upscale.factor > 4096
            or self.width * self.height * self.upscale.factor**2 > 16_000_000
        ):
            raise ValueError("upscale exceeds final dimension bound")
        if len({item.model_id for item in self.loras}) != len(self.loras):
            raise ValueError("loras must not repeat a model")
        if self.mode is ImageMode.TXT2IMG:
            if (
                self.init_image_id is not None
                or self.mask_id is not None
                or self.control is not None
            ):
                raise ValueError("txt2img cannot include init_image_id, mask_id or control")
            if self.strength is not None:
                raise ValueError("txt2img cannot include strength")
        elif self.mode is ImageMode.IMG2IMG:
            if self.init_image_id is None or self.strength is None:
                raise ValueError("img2img requires init_image_id and strength")
            if self.mask_id is not None or self.control is not None:
                raise ValueError("img2img cannot include mask_id or control")
        elif self.mode is ImageMode.FILL:
            if self.init_image_id is None or self.mask_id is None:
                raise ValueError("fill requires init_image_id and mask_id")
            if self.control is not None:
                raise ValueError("fill cannot include control")
        elif self.mode is ImageMode.CONTROL:
            if self.control is None:
                raise ValueError("control mode requires control")
            if (
                self.init_image_id is not None
                or self.mask_id is not None
                or self.strength is not None
            ):
                raise ValueError("control mode cannot include init_image_id, mask_id or strength")
        return self


def image_input_bindings(
    spec: ImageSpec,
) -> tuple[tuple[uuid.UUID, Literal["init", "mask", "control"]], ...]:
    """Return every generation input in stable lock order, rejecting reused IDs."""
    bindings: list[tuple[uuid.UUID, Literal["init", "mask", "control"]]] = []
    if spec.init_image_id is not None:
        bindings.append((spec.init_image_id, "init"))
    if spec.mask_id is not None:
        bindings.append((spec.mask_id, "mask"))
    if spec.control is not None:
        bindings.append((spec.control.image_id, "control"))
    if len({input_id for input_id, _ in bindings}) != len(bindings):
        raise ValueError("one image input cannot serve multiple purposes")
    return tuple(sorted(bindings, key=lambda item: item[0]))


class ImageCapabilityProfile(BaseModel):
    """Measured base-model bounds; checked after request and preset resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    modes: tuple[ImageMode, ...] = Field(min_length=1, max_length=4)
    min_width: int = Field(ge=64, le=4096, multiple_of=8)
    max_width: int = Field(ge=64, le=4096, multiple_of=8)
    min_height: int = Field(ge=64, le=4096, multiple_of=8)
    max_height: int = Field(ge=64, le=4096, multiple_of=8)
    max_pixels: int = Field(ge=4096, le=16_000_000)
    min_steps: int = Field(ge=1, le=150)
    max_steps: int = Field(ge=1, le=150)
    min_guidance: Decimal = Field(ge=0, le=30)
    max_guidance: Decimal = Field(ge=0, le=30)
    max_outputs: int = Field(ge=1, le=4)
    default_width: int | None = Field(default=None, ge=64, le=4096, multiple_of=8)
    default_height: int | None = Field(default=None, ge=64, le=4096, multiple_of=8)
    default_steps: int | None = Field(default=None, ge=1, le=150)
    default_guidance: Decimal | None = Field(default=None, ge=0, le=30)
    supports_negative_prompt: bool = False
    max_loras: int = Field(default=0, ge=0, le=4)
    required_dependency_ids: tuple[uuid.UUID, ...] = Field(default_factory=tuple, max_length=16)

    @model_validator(mode="after")
    def ordered_bounds(self) -> ImageCapabilityProfile:
        if (
            self.min_width > self.max_width
            or self.min_height > self.max_height
            or self.min_steps > self.max_steps
            or self.min_guidance > self.max_guidance
            or len(set(self.modes)) != len(self.modes)
        ):
            raise ValueError("capability bounds or modes are inconsistent")
        if len(set(self.required_dependency_ids)) != len(self.required_dependency_ids):
            raise ValueError("required dependencies cannot repeat")
        defaults = (
            self.default_width,
            self.default_height,
            self.default_steps,
            self.default_guidance,
        )
        if any(value is not None for value in defaults):
            if any(value is None for value in defaults):
                raise ValueError("image defaults must be complete")
            assert self.default_width is not None
            assert self.default_height is not None
            assert self.default_steps is not None
            assert self.default_guidance is not None
            if not self.min_width <= self.default_width <= self.max_width:
                raise ValueError("default_width exceeds model bounds")
            if not self.min_height <= self.default_height <= self.max_height:
                raise ValueError("default_height exceeds model bounds")
            if self.default_width * self.default_height > self.max_pixels:
                raise ValueError("default_width and default_height exceed max_pixels")
            if not self.min_steps <= self.default_steps <= self.max_steps:
                raise ValueError("default_steps exceeds model bounds")
            if not self.min_guidance <= self.default_guidance <= self.max_guidance:
                raise ValueError("default_guidance exceeds model bounds")
        return self

    def validate_spec(self, spec: ImageSpec) -> None:
        """Name the first violated measured limit; never clamp the request."""
        if spec.mode not in self.modes:
            raise ValueError("mode is unsupported")
        if not self.min_width <= spec.width <= self.max_width:
            raise ValueError("width exceeds model bounds")
        if not self.min_height <= spec.height <= self.max_height:
            raise ValueError("height exceeds model bounds")
        if spec.width * spec.height > self.max_pixels:
            raise ValueError("pixels exceed model bounds")
        if not self.min_steps <= spec.steps <= self.max_steps:
            raise ValueError("steps exceed model bounds")
        if not self.min_guidance <= spec.guidance <= self.max_guidance:
            raise ValueError("guidance exceeds model bounds")
        if spec.n > self.max_outputs:
            raise ValueError("n exceeds model bound")
        if len(spec.loras) > self.max_loras:
            raise ValueError("loras exceed model bound")
        if spec.negative_prompt is not None and not self.supports_negative_prompt:
            raise ValueError("negative_prompt is unsupported")


class ImageAdapterOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    display_name: str = Field(min_length=1, max_length=120)


class ImageModelOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    slug: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=120)
    capability: ImageCapabilityProfile
    required_dependency_count: int = Field(ge=0, le=16)
    loras: tuple[ImageAdapterOption, ...] = ()
    upscalers: tuple[ImageAdapterOption, ...] = ()
    residency: Literal["unknown"] = "unknown"


class ImageGenerationLimits(BaseModel):
    """Current operator policy, disclosed before accepting private image work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_input_max_bytes: int = Field(ge=1, le=10 * 1024**2)
    recipe_input_max_bytes: int = Field(ge=1, le=64 * 1024**2)
    output_max_bytes: int = Field(ge=1, le=64 * 1024**2)
    owner_storage_quota_bytes: int = Field(ge=1, le=5 * 1024**3)
    pending_per_owner: int = Field(ge=1, le=4)
    daily_outputs_per_owner: int = Field(ge=1, le=100)
    output_retention_hours: int | None = Field(default=None, ge=1, le=8760)


class ImageModelList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ImageModelOption] = Field(max_length=100)
    limits: ImageGenerationLimits | None = None


class ImageCoexistenceBounds(BaseModel):
    """Maximum image workload measured alongside the listed resident chat variants."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_width: int = Field(ge=64, le=2048)
    max_height: int = Field(ge=64, le=2048)
    max_steps: int = Field(ge=1, le=100)
    max_outputs: int = Field(ge=1, le=4)


class ImageLatencyVectorSample(BaseModel):
    """One bounded Prometheus instant-vector sample for live admission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: dict[str, str] = Field(max_length=8)
    value: tuple[float, str]


class ImageLatencyQueryData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resultType: Literal["vector"]
    result: tuple[ImageLatencyVectorSample, ...] = Field(max_length=1)


class ImageLatencyQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["success"]
    data: ImageLatencyQueryData


class ImageCoexistenceReportRequest(BaseModel):
    """Human-admin attested, same-node mixed-workload benchmark evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: uuid.UUID
    image_model_id: uuid.UUID
    image_mode: Literal["txt2img"] = "txt2img"
    chat_variant_ids: tuple[uuid.UUID, ...] = Field(min_length=1, max_length=8)
    hardware_fingerprint: str = Field(pattern=SHA256_PATTERN)
    runtime_fingerprint: str = Field(pattern=SHA256_PATTERN)
    measured_bounds: ImageCoexistenceBounds
    duration_seconds: int = Field(ge=900, le=86_400)
    prompt_tokens_max: int = Field(ge=4096, le=32768)
    first_token_p95_ms: float = Field(ge=0, le=1500, allow_inf_nan=False)
    gateway_overhead_p95_ms: float = Field(ge=0, le=20, allow_inf_nan=False)
    image_completed_count: int = Field(ge=1)
    image_progress_observed: Literal[True]
    swap_observed: Literal[False]
    thermal_alarm: Literal[False]
    runtime_version: Literal["mflux-0.20.0"]
    measured_at: AwareDatetime
    valid_until: AwareDatetime

    @model_validator(mode="after")
    def profile_window(self) -> ImageCoexistenceReportRequest:
        if len(set(self.chat_variant_ids)) != len(self.chat_variant_ids):
            raise ValueError("chat variants cannot repeat")
        if not self.measured_at < self.valid_until:
            raise ValueError("coexistence validity must follow measurement")
        return self


class ImageCoexistenceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    profile_hash: str = Field(pattern=SHA256_PATTERN)
    status: Literal["approved", "invalidated"]
    report: ImageCoexistenceReportRequest
    invalidated_at: AwareDatetime | None = None


class ImageSubmitRequest(BaseModel):
    """Root-level overrides; preset/default resolution produces an ImageSpec later."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    preset_id: uuid.UUID | None = None
    preset_revision: int | None = Field(default=None, ge=1)
    model_id: uuid.UUID | None = None
    variant_id: uuid.UUID | None = None
    mode: ImageMode | None = None
    prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    negative_prompt: str | None = Field(default=None, max_length=4000)
    width: int | None = Field(default=None, ge=64, le=4096, multiple_of=8)
    height: int | None = Field(default=None, ge=64, le=4096, multiple_of=8)
    steps: int | None = Field(default=None, ge=1, le=150)
    guidance: Decimal | None = Field(default=None, ge=0, le=30)
    seed: int | None = Field(default=None, ge=0, le=UINT32_MAX)
    n: int | None = Field(default=None, ge=1, le=4)
    loras: list[ImageLora] | None = Field(default=None, max_length=4)
    init_image_id: uuid.UUID | None = None
    strength: Decimal | None = Field(default=None, gt=0, le=1)
    mask_id: uuid.UUID | None = None
    control: ImageControl | None = None
    upscale: ImageUpscale | None = None
    output: ImageOutputSettings | None = None
    content_mode: ImageContentMode | None = None

    @model_validator(mode="after")
    def has_model_source(self) -> ImageSubmitRequest:
        if self.model_id is None and self.preset_id is None:
            raise ValueError("model_id or preset_id is required")
        if self.preset_revision is not None and self.preset_id is None:
            raise ValueError("preset_revision requires preset_id")
        return self


class ImageInputUpload(BaseModel):
    """Metadata for bounded multipart upload; bytes are streamed separately."""

    model_config = ConfigDict(extra="forbid")

    purpose: Literal["init", "mask", "control", "recipe"]
    filename: str = Field(min_length=1, max_length=255)
    byte_count: int = Field(ge=1, le=RECIPE_INPUT_MAX_BYTES)

    @field_validator("filename")
    @classmethod
    def safe_basename(cls, value: str) -> str:
        if (
            value in {".", ".."}
            or ".." in value
            or "/" in value
            or "\\" in value
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("filename must be a safe basename")
        if not value.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            raise ValueError("filename must be a supported still image")
        return value

    @model_validator(mode="after")
    def purpose_limit(self) -> ImageInputUpload:
        if self.purpose != "recipe" and self.byte_count > GENERATION_INPUT_MAX_BYTES:
            raise ValueError("byte_count exceeds 10 MiB generation input limit")
        if self.purpose == "recipe" and not self.filename.lower().endswith(".png"):
            raise ValueError("recipe filename must end in .png")
        return self


class ImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    purpose: Literal["init", "mask", "control", "recipe"]
    state: Literal["uploading", "processing", "ready", "failed", "deleting", "purged"]
    byte_count: int = Field(ge=1, le=RECIPE_INPUT_MAX_BYTES)
    sha256: str = Field(pattern=SHA256_PATTERN)
    width: int | None = Field(default=None, ge=1, le=4096)
    height: int | None = Field(default=None, ge=1, le=4096)
    safe_error: str | None = Field(default=None, max_length=200)
    created_at: datetime

    @model_validator(mode="after")
    def purpose_limit(self) -> ImageInput:
        if self.purpose != "recipe" and self.byte_count > GENERATION_INPUT_MAX_BYTES:
            raise ValueError("byte_count exceeds 10 MiB generation input limit")
        if self.purpose == "recipe" and (self.width is not None or self.height is not None):
            raise ValueError("recipe input cannot be promoted to source image")
        return self


class ImageRecipeImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    replacement_inputs: dict[str, uuid.UUID] = Field(default_factory=dict, max_length=3)

    @field_validator("replacement_inputs")
    @classmethod
    def digest_keys(cls, value: dict[str, uuid.UUID]) -> dict[str, uuid.UUID]:
        if any(not re.fullmatch(SHA256_PATTERN, digest) for digest in value):
            raise ValueError("replacement input keys must be SHA-256 digests")
        return value


class ImageManifestDigest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    slug: str | None = Field(default=None, pattern=SLUG_PATTERN)
    revision: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    sha256: str = Field(pattern=SHA256_PATTERN)


class ImageInputDigest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_id: uuid.UUID
    sha256: str = Field(pattern=SHA256_PATTERN)
    width: int = Field(ge=1, le=4096)
    height: int = Field(ge=1, le=4096)


class ResolvedImageSpec(BaseModel):
    """Immutable execution facts; no owner, path, URL or secret field exists."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    spec: ImageSpec
    seeds: tuple[int, ...] = Field(min_length=1, max_length=4)
    pipeline_version: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    environment_fingerprint: str = Field(pattern=SHA256_PATTERN)
    model_sha256: str = Field(pattern=SHA256_PATTERN)
    dependencies: tuple[ImageManifestDigest, ...] = Field(default_factory=tuple, max_length=16)
    inputs: tuple[ImageInputDigest, ...] = Field(default_factory=tuple, max_length=3)
    preset_id: uuid.UUID | None = None
    preset_revision: int | None = Field(default=None, ge=1)
    spec_hash: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def seeds_match(self) -> ResolvedImageSpec:
        if self.spec.seed is None:
            raise ValueError("resolved spec requires an effective seed")
        if len(self.seeds) != self.spec.n or any(
            seed < 0 or seed > UINT32_MAX for seed in self.seeds
        ):
            raise ValueError("seeds must match output count and unsigned 32-bit bounds")
        if self.seeds != tuple(expand_image_seeds(self.spec.seed, self.spec.n)):
            raise ValueError("seeds must expand from the resolved seed")
        if self.preset_revision is not None and self.preset_id is None:
            raise ValueError("preset_revision requires preset_id")
        if len({item.model_id for item in self.dependencies}) != len(self.dependencies):
            raise ValueError("dependencies cannot repeat model_id")
        if len({item.input_id for item in self.inputs}) != len(self.inputs):
            raise ValueError("inputs cannot repeat input_id")
        if self.spec_hash != canonical_spec_hash(self.spec):
            raise ValueError("spec_hash differs from canonical spec")
        return self


class ImageRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    resolved: ResolvedImageSpec
    output_index: int = Field(ge=0, le=3)
    seed: int = Field(ge=0, le=UINT32_MAX)
    pixel_sha256: str = Field(pattern=SHA256_PATTERN)
    width: int = Field(ge=1, le=4096)
    height: int = Field(ge=1, le=4096)

    @model_validator(mode="after")
    def matches_resolved(self) -> ImageRecipe:
        if self.output_index >= len(self.resolved.seeds):
            raise ValueError("output_index exceeds resolved outputs")
        if self.seed != self.resolved.seeds[self.output_index]:
            raise ValueError("seed differs from resolved output seed")
        factor = self.resolved.spec.upscale.factor if self.resolved.spec.upscale else 1
        if (
            self.width != self.resolved.spec.width * factor
            or self.height != self.resolved.spec.height * factor
        ):
            raise ValueError("output dimensions differ from resolved spec")
        return self


class ImageJobSettingsSnapshot(BaseModel):
    """Persisted settings before and after a Studio runtime is selected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    effective_spec: ImageSpec
    resolved: ResolvedImageSpec | None = None

    @model_validator(mode="after")
    def matching_spec(self) -> ImageJobSettingsSnapshot:
        if self.resolved is not None and self.resolved.spec != self.effective_spec:
            raise ValueError("resolved spec differs from effective_spec")
        return self

    def bind(self, resolved: ResolvedImageSpec) -> ImageJobSettingsSnapshot:
        if self.resolved is not None:
            if self.resolved == resolved:
                return self
            raise ValueError("runtime already bound")
        if resolved.spec != self.effective_spec:
            raise ValueError("resolved spec differs from effective_spec")
        return ImageJobSettingsSnapshot(effective_spec=self.effective_spec, resolved=resolved)


class ImageJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ULID_PATTERN)
    state: ImageJobState
    effective_spec: ImageSpec
    resolved: ResolvedImageSpec | None
    queue_position: int | None = Field(default=None, ge=0)
    progress_step: int | None = Field(default=None, ge=0)
    failure_code: str | None = Field(default=None, max_length=100)
    latest_event_sequence: int = Field(default=0, ge=0)
    outputs: list[ImageOutput] = Field(default_factory=list, max_length=4)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def runtime_state(self) -> ImageJob:
        if self.resolved is not None and self.resolved.spec != self.effective_spec:
            raise ValueError("resolved spec differs from effective_spec")
        if (
            self.state
            in {
                ImageJobState.RUNNING,
                ImageJobState.TRANSFERRING,
                ImageJobState.SUCCEEDED,
            }
            and self.resolved is None
        ):
            raise ValueError("resolved runtime is required for active or completed job")
        if self.outputs and self.state is not ImageJobState.SUCCEEDED:
            raise ValueError("only succeeded jobs may expose published outputs")
        return self


class ImageJobReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(pattern=ULID_PATTERN)
    state: ImageJobState
    queue_position: int | None = Field(default=None, ge=0)
    event_cursor: str | None = Field(default=None, max_length=50)


class ImageJobPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ImageJob] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=100)


class ImageJobEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(pattern=ULID_PATTERN)
    sequence: int = Field(ge=1)
    at: datetime
    type: Literal["queued", "started", "progress", "done", "error", "cancelled", "reset"]
    state: ImageJobState
    queue_position: int | None = Field(default=None, ge=0)
    stage: str | None = Field(default=None, max_length=80)
    output_index: int | None = Field(default=None, ge=0, le=3)
    step: int | None = Field(default=None, ge=0)
    total_steps: int | None = Field(default=None, ge=1)
    safe_code: str | None = Field(default=None, max_length=100)
    outputs: list[ImageOutput] | None = Field(default=None, max_length=4)
    snapshot: ImageJob | None = None

    @model_validator(mode="after")
    def event_matches_state(self) -> ImageJobEvent:
        required = {
            "queued": ImageJobState.QUEUED,
            "started": ImageJobState.RUNNING,
            "progress": ImageJobState.RUNNING,
            "done": ImageJobState.SUCCEEDED,
            "error": ImageJobState.FAILED,
            "cancelled": ImageJobState.CANCELLED,
        }
        if self.type in required and self.state is not required[self.type]:
            raise ValueError("event type and state differ")
        if self.type == "progress" and (self.stage is None or self.step is None):
            raise ValueError("progress requires stage and step")
        if self.type == "error" and self.safe_code is None:
            raise ValueError("error requires safe_code")
        if self.type == "done":
            if (
                not self.outputs
                or any(output.job_id != self.job_id for output in self.outputs)
                or sorted(output.index for output in self.outputs) != list(range(len(self.outputs)))
            ):
                raise ValueError("done requires this job's complete output batch")
        elif self.outputs is not None:
            raise ValueError("only done may carry outputs")
        if self.type == "reset":
            if self.snapshot is None or self.snapshot.id != self.job_id:
                raise ValueError("reset requires this job snapshot")
            if self.snapshot.latest_event_sequence != self.sequence:
                raise ValueError("reset cursor differs from snapshot")
        elif self.snapshot is not None:
            raise ValueError("only reset may carry a job snapshot")
        return self


class ImageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    job_id: str = Field(pattern=ULID_PATTERN)
    index: int = Field(ge=0, le=3)
    recipe: ImageRecipe
    tag: ImageContentTag
    classifier_diagnostic: ImageClassifierDiagnostic | None = None
    byte_count: int = Field(ge=1, le=RECIPE_INPUT_MAX_BYTES)
    file_sha256: str = Field(pattern=SHA256_PATTERN)
    created_at: datetime


class ImageOutputPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ImageOutput] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=100)


class ImagePreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    prompt_prefix: str = Field(default="", max_length=1000)
    defaults: ImageSubmitRequest
    retired: bool = False

    @model_validator(mode="after")
    def no_nested_preset(self) -> ImagePreset:
        if self.defaults.preset_id is not None or self.defaults.model_id is None:
            raise ValueError("preset defaults require a direct registry model_id")
        return self


class ImagePresetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    prompt_prefix: str = Field(default="", max_length=1000)
    defaults: ImageSubmitRequest

    @model_validator(mode="after")
    def no_nested_preset(self) -> ImagePresetCreate:
        if self.defaults.preset_id is not None or self.defaults.model_id is None:
            raise ValueError("preset defaults require a direct registry model_id")
        return self


class ImagePresetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    prompt_prefix: str | None = Field(default=None, max_length=1000)
    defaults: ImageSubmitRequest | None = None


class ImagePresetList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ImagePreset] = Field(max_length=100)


class ImageDownloadGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_id: uuid.UUID
    url: str = Field(min_length=1, max_length=500)
    expires_at: datetime


class ImageDeletionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_id: uuid.UUID
    state: Literal["tombstoned", "purged"]


class ImageRecipeImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe: ImageRecipe
    settings: ImageSubmitRequest
    exact_reproduction_available: bool
    missing_input_sha256: list[str] = Field(default_factory=list, max_length=3)
    missing_dependency_sha256: list[str] = Field(default_factory=list, max_length=16)
    unavailable_reason: str | None = Field(default=None, max_length=200)

    @field_validator("missing_input_sha256", "missing_dependency_sha256")
    @classmethod
    def valid_missing_digests(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(SHA256_PATTERN, digest) for digest in value):
            raise ValueError("missing digests must be SHA-256")
        return value

    @model_validator(mode="after")
    def availability_matches_reasons(self) -> ImageRecipeImport:
        if self.exact_reproduction_available and (
            self.missing_input_sha256
            or self.missing_dependency_sha256
            or self.unavailable_reason is not None
        ):
            raise ValueError("exact reproduction cannot have missing inputs or dependencies")
        if not self.exact_reproduction_available and not (
            self.missing_input_sha256 or self.missing_dependency_sha256 or self.unavailable_reason
        ):
            raise ValueError("unavailable reproduction needs a reason")
        return self


class OpenAIImageGenerationRequest(BaseModel):
    """Standard image fields plus prefixed Coire extensions."""

    model_config = ConfigDict(extra="forbid")

    model: uuid.UUID
    prompt: str = Field(min_length=1, max_length=4000)
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = Field(default=None, pattern=r"^[0-9]{2,4}x[0-9]{2,4}$")
    quality: Literal["auto", "standard", "hd", "low", "medium", "high"] | None = None
    response_format: Literal["url", "b64_json"] = "url"
    output_format: Literal["png"] = "png"
    stream: Literal[False] = False
    user: str | None = Field(default=None, max_length=128)
    coire_preset_id: uuid.UUID | None = None
    coire_preset_revision: int | None = Field(default=None, ge=1)
    coire_seed: int | None = Field(default=None, ge=0, le=UINT32_MAX)
    coire_content_mode: ImageContentMode | None = None

    @model_validator(mode="after")
    def valid_size_and_preset(self) -> OpenAIImageGenerationRequest:
        if self.coire_preset_revision is not None and self.coire_preset_id is None:
            raise ValueError("coire_preset_revision requires coire_preset_id")
        if self.size is not None:
            width, height = map(int, self.size.split("x"))
            if not 64 <= width <= 4096 or not 64 <= height <= 4096:
                raise ValueError("size exceeds image dimensions")
            if width % 8 or height % 8 or width * height > 16_000_000:
                raise ValueError("size violates alignment or pixel bound")
        return self


class OpenAIImageData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    b64_json: str | None = None

    @model_validator(mode="after")
    def one_format(self) -> OpenAIImageData:
        if (self.url is None) == (self.b64_json is None):
            raise ValueError("exactly one of url or b64_json is required")
        return self


class OpenAIImageGenerationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created: int = Field(ge=0)
    data: list[OpenAIImageData] = Field(min_length=1, max_length=4)
    coire_job_id: str = Field(pattern=ULID_PATTERN)


def expand_image_seeds(seed: int, count: int) -> list[int]:
    """Resolve a batch from one effective seed, wrapping at 32 bits."""
    if not 0 <= seed <= UINT32_MAX or not 1 <= count <= 4:
        raise ValueError("seed/count outside image bounds")
    return [(seed + index) % (UINT32_MAX + 1) for index in range(count)]


def _canonical_bytes(value: BaseModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def canonical_spec_hash(spec: ImageSpec) -> str:
    """Digest the precise versioned effective settings without runtime identity."""
    return hashlib.sha256(_canonical_bytes(spec)).hexdigest()


def canonical_client_intent_hash(request: ImageSubmitRequest) -> str:
    """Hash stable client intent before mutable defaults or random seed resolution."""
    return hashlib.sha256(_canonical_bytes(request)).hexdigest()


def canonical_recipe_bytes(recipe: ImageRecipe) -> bytes:
    """Encode the exact bounded recipe for database and uncompressed PNG iTXt."""
    encoded = _canonical_bytes(recipe)
    if len(encoded) > RECIPE_METADATA_MAX_BYTES:
        raise ValueError("recipe exceeds 64 KiB metadata bound")
    return encoded


def pixel_digest(pixels: bytes, *, width: int, height: int, channels: int) -> str:
    """Hash decoded pixels and shape, independently of PNG encoding metadata."""
    if width < 1 or height < 1 or channels not in {1, 3, 4}:
        raise ValueError("invalid pixel shape")
    if len(pixels) != width * height * channels:
        raise ValueError("decoded pixel byte count does not match shape")
    digest = hashlib.sha256()
    digest.update(f"{width}x{height}x{channels}:".encode())
    digest.update(pixels)
    return digest.hexdigest()
