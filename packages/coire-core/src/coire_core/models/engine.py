"""Engine process wire shapes.

An `EngineProcess` is one `mlx_lm.server` the node agent owns. Feature 005 generalises this
into `ModelInstance`; the fields here are the subset that instance needs, named so they
survive the move.

The identity that matters is `(pid, process_create_time)`. A pid alone is not an identity —
it is reused — and after an agent restart the agent has to decide whether the process holding
a pid is the engine it started or something else entirely (spec FR-015, research R4).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EngineState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    """Reached only when a generation request the agent issued succeeded (spec FR-012). The
    engine's own liveness endpoint answers before the weights are loaded, so it cannot mean
    this."""
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    ORPHAN = "orphan"
    """A running engine matching no expectation. Reported, never silently adopted or killed."""


TERMINAL_ENGINE_STATES = frozenset({EngineState.STOPPED, EngineState.FAILED})
LIVE_ENGINE_STATES = frozenset({EngineState.STARTING, EngineState.READY, EngineState.STOPPING})
"""States that hold memory, and therefore count against a node's budget (spec FR-020)."""


class EngineStatus(BaseModel):
    """A node's view of one engine."""

    model_config = ConfigDict(extra="forbid")

    engine_id: uuid.UUID | None = None
    """None for an orphan: it matches no registry row by definition."""
    slug: str | None = None
    port: int
    pid: int | None = None
    process_create_time: float | None = None
    state: EngineState
    state_reason: str | None = None
    exit_code: int | None = None
    exit_output: str | None = None
    """Last 4 KiB of stderr when a start failed — the engine's own account of why (US3
    scenario 4). Truncated because a traceback is diagnostic and a log flood is not."""
    estimate_bytes: int = 0
    resident_bytes: int | None = None
    resident_delta_bytes: int | None = None
    cpu_percent: float | None = None
    chat_template_sha256: str | None = None
    load_seconds: float | None = None
    last_health_at: datetime | None = None
    started_at: datetime
    stopped_at: datetime | None = None


class EngineProcess(BaseModel):
    """The control plane's persisted view of an engine."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    model_id: uuid.UUID | None = None
    """None for an orphan whose slug matches no model."""
    node: str
    port: int
    pid: int | None = None
    state: EngineState
    state_reason: str | None = None
    estimate_bytes: int = 0
    resident_bytes: int | None = None
    resident_delta_bytes: int | None = None
    cpu_percent: float | None = None
    chat_template_sha256: str | None = None
    last_health_at: datetime | None = None
    started_at: datetime
    stopped_at: datetime | None = None


class EngineStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine_id: uuid.UUID
    slug: str
    estimate_bytes: int = Field(ge=1)
    chat_template: str | None = None
    """Registry-supplied override, written to a file beside the copy and passed as
    `--chat-template`. Never caller-derived (spec FR-017)."""


class BudgetRefused(BaseModel):
    """Why a load was refused for memory (spec FR-020). Carries the figures so the refusal is
    actionable rather than a bare 409."""

    model_config = ConfigDict(extra="forbid")

    reason: str = "budget"
    required_bytes: int
    committed_bytes: int
    budget_bytes: int


class ReconcileExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine_id: uuid.UUID
    slug: str
    port: int
    pid: int | None = None
    process_create_time: float | None = None


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected: list[ReconcileExpectation] = Field(default_factory=list)


class ReconcileResult(BaseModel):
    """What the node actually runs, against what the registry expected (spec FR-015)."""

    model_config = ConfigDict(extra="forbid")

    adopted: list[EngineStatus] = Field(default_factory=list)
    dead: list[uuid.UUID] = Field(default_factory=list)
    orphans: list[EngineStatus] = Field(default_factory=list)
