"""Model registry wire shapes.

The registry record is the platform's answer to "what models exist, who may see them, and
where do they live". Principle V fixes most of this shape: placement policy, memory estimate,
idle TTL, chat template, visibility, and a capability profile from which harness behaviour is
selected — never from a model name.

One identifier rule runs through the whole feature: a caller names a model by its registry
`id` and nothing else. The `slug` is the platform's own store key, derived from the repository
id, and the only thing ever handed to an engine (spec FR-017).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

REPO_ID_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
SLUG_PATTERN = r"^[A-Za-z0-9_.-]+--[A-Za-z0-9_.-]+$"
PLACEMENT_PATTERN = r"^(single:(auto|coire-[a-z0-9-]+)|pinned:coire-[a-z0-9-]+|sharded:(tp|pp))$"
MAX_CHAT_TEMPLATE_BYTES = 64 * 1024

_slug_re = re.compile(SLUG_PATTERN)


def slug_for(repo_id: str) -> str:
    """The store key for a repository.

    `org/name` becomes `org--name`: one flat directory per model, no nesting, and no `/` that
    could escape the store root when joined to a path.
    """
    if not re.match(REPO_ID_PATTERN, repo_id):
        raise ValueError(f"not a Hugging Face repo id: {repo_id!r}")
    return repo_id.replace("/", "--")


def is_valid_slug(slug: str) -> bool:
    """Whether a string is a slug this platform could have produced.

    Used at every point a slug arrives from outside the process. `..` and `/` cannot match.
    """
    return bool(_slug_re.match(slug))


class ModelState(StrEnum):
    DOWNLOADING = "downloading"
    REPLICATING = "replicating"
    READY = "ready"
    FAILED = "failed"
    RETIRED = "retired"


class Visibility(StrEnum):
    ADMIN_ONLY = "admin_only"
    PUBLISHED = "published"


class Tag(StrEnum):
    """The picker's grouping vocabulary (ARCHITECTURE.md section 3.2)."""

    CODING = "coding"
    GENERAL = "general"
    REASONING = "reasoning"
    VISION = "vision"
    IMAGE = "image"


class ToolCalling(StrEnum):
    NONE = "none"
    PROMPTED = "prompted"
    NATIVE = "native"


class StructuredOutput(StrEnum):
    NONE = "none"
    JSON_MODE = "json_mode"
    JSON_SCHEMA = "json_schema"


class Reasoning(StrEnum):
    NONE = "none"
    THINKING = "thinking"
    HYBRID = "hybrid"


class LoadState(StrEnum):
    LOADED = "loaded"
    LOADING = "loading"
    COLD = "cold"


class CopyRole(StrEnum):
    """Which copy came from Hugging Face. Exactly one `origin` per model proves the
    pull-once rule held (spec SC-004)."""

    ORIGIN = "origin"
    REPLICA = "replica"


class RejectionReason(StrEnum):
    NOT_MLX_FORMAT = "not_mlx_format"
    NO_FIT_MEMORY = "no_fit_memory"
    NO_FIT_DISK = "no_fit_disk"
    GATED = "gated"
    NOT_FOUND = "not_found"
    INSPECT_FAILED = "inspect_failed"


class LoadRefusalReason(StrEnum):
    NOT_READY = "not_ready"
    BUDGET = "budget"
    NODE_UNREACHABLE = "node_unreachable"


class CapabilityProfile(BaseModel):
    """Declared model behaviour. Harness behaviour is selected from this, never from a model
    name (Principle V)."""

    model_config = ConfigDict(extra="forbid")

    tool_calling: ToolCalling = ToolCalling.NONE
    structured_output: StructuredOutput = StructuredOutput.NONE
    context_window: int | None = Field(default=None, ge=1)
    reasoning: Reasoning = Reasoning.NONE
    parallel_tools: bool = False
    chat_template_present: bool = False
    verified: bool = False
    """Set only by feature 017's harness evaluation. The router refuses unverified models for
    write-capable tasks, so this is never editable through the curation API."""


class CapabilityProfileUpdate(BaseModel):
    """The admin-editable subset. `verified` is absent by construction, so a request carrying
    it is rejected as an unknown field rather than silently ignored."""

    model_config = ConfigDict(extra="forbid")

    tool_calling: ToolCalling | None = None
    structured_output: StructuredOutput | None = None
    context_window: int | None = Field(default=None, ge=1)
    reasoning: Reasoning | None = None
    parallel_tools: bool | None = None


class ModelCopy(BaseModel):
    """A model's presence on one node."""

    model_config = ConfigDict(extra="forbid")

    node: str
    path: str
    bytes: int
    manifest_sha256: str | None = None
    verified: bool = False
    verified_at: datetime | None = None
    mismatched_paths: list[str] = Field(default_factory=list)
    role: CopyRole


class ModelAddRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str = Field(pattern=REPO_ID_PATTERN)
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    tags: list[Tag] = Field(default_factory=list)
    placement_policy: str = Field(default="single:auto", pattern=PLACEMENT_PATTERN)
    idle_ttl_seconds: int | None = Field(default=None, ge=60)


class ModelUpdateRequest(BaseModel):
    """Curation. Every field is optional; absent means unchanged.

    `chat_template` is tri-state and cannot be expressed with a plain `| None` default, so
    absence is spelled with a sentinel: the field is `str | None` and "not supplied" is
    detected with `model_fields_set`.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    visibility: Visibility | None = None
    entitlement: list[str] | None = None
    tags: list[Tag] | None = None
    placement_policy: str | None = Field(default=None, pattern=PLACEMENT_PATTERN)
    idle_ttl_seconds: int | None = Field(default=None, ge=60)
    chat_template: str | None = None
    capability_profile: CapabilityProfileUpdate | None = None

    @field_validator("chat_template")
    @classmethod
    def _template_within_bounds(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode()) > MAX_CHAT_TEMPLATE_BYTES:
            raise ValueError(
                f"chat_template exceeds {MAX_CHAT_TEMPLATE_BYTES} bytes; it is a chat template, "
                "not a payload"
            )
        return value


class Model(BaseModel):
    """The registry record."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    repo_id: str
    slug: str
    display_name: str
    description: str | None = None
    state: ModelState
    state_reason: str | None = None
    visibility: Visibility = Visibility.ADMIN_ONLY
    entitlement: list[str] = Field(default_factory=list)
    tags: list[Tag] = Field(default_factory=list)
    placement_policy: str = Field(pattern=PLACEMENT_PATTERN)
    precision: str
    weight_bytes: int
    total_bytes: int
    file_count: int
    memory_estimate_bytes: int
    idle_ttl_seconds: int | None = None
    context_window: int | None = None
    chat_template: str | None = None
    capability_profile: CapabilityProfile = Field(default_factory=CapabilityProfile)
    manifest_sha256: str | None = None
    created_at: datetime
    updated_at: datetime
    ready_at: datetime | None = None


class ModelListing(BaseModel):
    """The user-facing shape.

    Deliberately narrow: no paths, no copies, no failure reasons, no repo id. What a user may
    see about a model is what the picker needs to choose one.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    display_name: str
    description: str | None = None
    tags: list[Tag] = Field(default_factory=list)
    context_window: int | None = None
    precision: str
    load_state: LoadState
    loaded_on: list[str] = Field(default_factory=list)
    estimated_warmup_seconds: float | None = None
    capability_profile: CapabilityProfile


class ModelRejected(BaseModel):
    """Why an add was refused before any bytes moved (spec FR-010)."""

    model_config = ConfigDict(extra="forbid")

    reason: RejectionReason
    message: str
    required_bytes: int | None = None
    available_bytes: int | None = None
    detail: dict[str, object] = Field(default_factory=dict)


class LoadRefused(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: LoadRefusalReason
    message: str
    node: str | None = None
    required_bytes: int | None = None
    committed_bytes: int | None = None
    budget_bytes: int | None = None
