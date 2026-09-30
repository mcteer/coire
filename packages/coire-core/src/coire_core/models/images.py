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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from coire_core.models.files import SHA256_PATTERN, ULID_PATTERN

UINT32_MAX = 2**32 - 1
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
    supports_negative_prompt: bool = False
    max_loras: int = Field(default=0, ge=0, le=4)

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


class ImageJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ULID_PATTERN)
    state: ImageJobState
    resolved: ResolvedImageSpec
    queue_position: int | None = Field(default=None, ge=0)
    progress_step: int | None = Field(default=None, ge=0)
    failure_code: str | None = Field(default=None, max_length=100)
    latest_event_sequence: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime


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
        return self


class ImageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    job_id: str = Field(pattern=ULID_PATTERN)
    index: int = Field(ge=0, le=3)
    recipe: ImageRecipe
    tag: ImageContentTag
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
