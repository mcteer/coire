"""Typed memory-ledger and placement contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coire_core.models.node import Reachability


class ReservationHolder(StrEnum):
    SANDBOX = "sandbox"
    MODEL = "model"
    CONVERSION = "conversion"
    TRAINING = "training"
    IMAGE = "image"
    RUN = "run"


class MemoryReservationState(StrEnum):
    PENDING = "pending"
    HELD = "held"
    RELEASING = "releasing"
    RELEASED = "released"
    FAILED = "failed"


class PlacementState(StrEnum):
    REQUESTED = "requested"
    WAITING_FOR_DRAIN = "waiting_for_drain"
    EVICTING = "evicting"
    RESERVING = "reserving"
    LOADING = "loading"
    READY = "ready"
    REFUSED = "refused"
    FAILED = "failed"


class OccupantReason(StrEnum):
    PINNED = "pinned"
    IN_USE = "in_use"
    ELIGIBLE = "eligible"


class MemoryReservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    node_id: uuid.UUID
    holder_type: ReservationHolder
    holder_id: str
    bytes: int = Field(gt=0)
    pinned: bool = False
    state: MemoryReservationState
    last_used_at: datetime
    created_at: datetime
    released_at: datetime | None = None
    in_flight: int = Field(default=0, ge=0)


class MemoryLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: uuid.UUID
    node_name: str
    budget_bytes: int = Field(gt=0)
    sandbox_bytes: int = Field(ge=0)
    reserved_bytes: int = Field(ge=0)
    free_bytes: int
    measured_resident_bytes: int | None = Field(default=None, ge=0)
    drift_ratio: float | None = None
    health: Reachability
    health_reason: str | None = None
    health_sampled_at: datetime | None = None
    reservations: list[MemoryReservation] = Field(default_factory=list)
    updated_at: datetime

    @model_validator(mode="after")
    def totals_reconcile(self) -> MemoryLedger:
        if self.free_bytes != self.budget_bytes - self.reserved_bytes:
            raise ValueError("free_bytes must equal budget_bytes minus reserved_bytes")
        return self


class LedgerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_bytes: int | None = Field(default=None, gt=0)
    sandbox_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def has_change(self) -> LedgerUpdate:
        if self.budget_bytes is None and self.sandbox_bytes is None:
            raise ValueError("at least one ledger value is required")
        return self


class PlacementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_id: uuid.UUID
    policy: str | None = Field(
        default=None,
        pattern=r"^(single:(auto|coire-[a-z0-9-]+)|pinned:coire-[a-z0-9-]+)$",
    )


class PlacementOccupant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reservation_id: uuid.UUID
    holder_id: str
    bytes: int = Field(gt=0)
    reason: OccupantReason


class PlacementDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    model_id: uuid.UUID
    variant_id: uuid.UUID
    policy: str
    required_bytes: int = Field(gt=0)
    state: PlacementState
    selected_node_id: uuid.UUID | None = None
    evicted_reservation_ids: list[uuid.UUID] = Field(default_factory=list)
    refusal_code: str | None = None
    refusal_detail: str | None = None
    occupants: list[PlacementOccupant] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PinUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned: bool
