"""Immutable base/adapter target identities and admin adapter lifecycle contracts."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from coire_core.models.registry import Visibility
from coire_core.models.training_types import AdapterSlug, Digest, TrainingId, TrainingWire

PAIR_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}@[a-z0-9][a-z0-9-]{0,62}$"
type AdapterSelector = Annotated[str, Field(pattern=PAIR_PATTERN)]
type ModelSelector = uuid.UUID | AdapterSelector


class InferenceTarget(TrainingWire):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    model_id: uuid.UUID
    variant_id: uuid.UUID
    adapter_id: uuid.UUID | None = None
    base_manifest_sha256: Digest
    adapter_manifest_sha256: Digest | None = None

    @model_validator(mode="after")
    def exact_artifacts(self) -> InferenceTarget:
        if (self.adapter_id is None) != (self.adapter_manifest_sha256 is None):
            raise ValueError("adapter identity and manifest must be supplied together")
        return self


class AdapterState(StrEnum):
    VALIDATING = "validating"
    REPLICATING = "replicating"
    READY = "ready"
    FAILED = "failed"
    RETIRED = "retired"


def validate_transport_target(
    model_id: uuid.UUID,
    variant_id: uuid.UUID,
    target: InferenceTarget | None,
    selector: ModelSelector | None,
) -> None:
    """Bind scheduler-authored routing selectors to the exact evaluated subject."""
    if target is not None and (target.model_id != model_id or target.variant_id != variant_id):
        raise ValueError("transport target differs from its model or variant")
    is_pair = isinstance(selector, str) and "@" in selector
    if is_pair != (target is not None and target.adapter_id is not None):
        raise ValueError("adapter target and pair selector must be supplied together")
    if selector is not None:
        parent = uuid.UUID(str(selector).split("@", 1)[0])
        if parent != model_id:
            raise ValueError("public selector differs from the target parent")


class AdapterDetail(TrainingWire):
    id: uuid.UUID
    model_id: uuid.UUID
    base_variant_id: uuid.UUID
    slug: AdapterSlug
    selector: AdapterSelector
    state: AdapterState
    visibility: Visibility = Visibility.ADMIN_ONLY
    manifest_sha256: Digest | None = None
    base_manifest_sha256: Digest
    source_job_id: TrainingId
    source_checkpoint_id: uuid.UUID
    resolved_spec_sha256: Digest
    parameterization: Literal["lora", "qlora", "dora"]
    objective: Literal["sft"] = "sft"
    verified: bool = False
    evaluation_id: uuid.UUID | None = None
    version: int = Field(ge=1)
    created_at: AwareDatetime
    required_entitlements: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def selector_matches(self) -> AdapterDetail:
        if self.selector != f"{self.model_id}@{self.slug}":
            raise ValueError("adapter selector differs from registered model and slug")
        if self.state is AdapterState.READY and self.manifest_sha256 is None:
            raise ValueError("ready adapter requires an immutable manifest")
        if self.verified and self.evaluation_id is None:
            raise ValueError("verified adapter requires independent evaluation identity")
        if self.visibility is Visibility.PUBLISHED and self.state is not AdapterState.READY:
            raise ValueError("only ready adapters may be published")
        return self


class AdapterCurationRequest(TrainingWire):
    expected_version: int = Field(strict=True, ge=1)
    visibility: Visibility


class AdapterRetireRequest(TrainingWire):
    expected_version: int = Field(strict=True, ge=1)


class AdapterReceipt(TrainingWire):
    adapter_id: uuid.UUID
    state: AdapterState
    version: int = Field(ge=1)


class AdapterPage(TrainingWire):
    items: list[AdapterDetail] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)
