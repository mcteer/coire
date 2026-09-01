"""Strict contracts for the isolated ops harness and human confirmation boundary."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class OpsSessionState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class OpsConversationState(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class OpsProposalState(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    EXECUTED = "executed"
    DECLINED = "declined"
    EXPIRED = "expired"
    STALE = "stale"
    FAILED = "failed"


class OpsTurnStatus(StrEnum):
    ANSWERED = "answered"
    PROPOSED = "proposed"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class OpsMessageRole(StrEnum):
    ADMIN = "admin"
    OPS = "ops"
    SYSTEM = "system"


class OpsActionPrecondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_version: str = Field(min_length=1, max_length=128)
    expected_state: str = Field(min_length=1, max_length=64)


class EmptyOpsParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstanceLoadParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_id: uuid.UUID
    policy: str | None = Field(default=None, max_length=64)


class InstanceUnloadAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["instance.unload"]
    target_type: Literal["instance"]
    target_id: uuid.UUID
    parameters: EmptyOpsParameters = Field(default_factory=EmptyOpsParameters)
    precondition: OpsActionPrecondition


class RunKillAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["run.kill"]
    target_type: Literal["run"]
    target_id: uuid.UUID
    parameters: EmptyOpsParameters = Field(default_factory=EmptyOpsParameters)
    precondition: OpsActionPrecondition


class ModelPinAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["model.pin"]
    target_type: Literal["model"]
    target_id: uuid.UUID
    parameters: EmptyOpsParameters = Field(default_factory=EmptyOpsParameters)
    precondition: OpsActionPrecondition


class ModelUnpinAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["model.unpin"]
    target_type: Literal["model"]
    target_id: uuid.UUID
    parameters: EmptyOpsParameters = Field(default_factory=EmptyOpsParameters)
    precondition: OpsActionPrecondition


class InstanceLoadAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["instance.load"]
    target_type: Literal["model"]
    target_id: uuid.UUID
    parameters: InstanceLoadParameters
    precondition: OpsActionPrecondition


ResolvedOpsAction = Annotated[
    InstanceUnloadAction | RunKillAction | ModelPinAction | ModelUnpinAction | InstanceLoadAction,
    Field(discriminator="operation"),
]
resolved_ops_action_adapter: TypeAdapter[ResolvedOpsAction] = TypeAdapter(ResolvedOpsAction)


class OpsSessionRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID
    service_instance: str = Field(min_length=1, max_length=128)


class OpsSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    service_instance: str
    state: OpsSessionState
    started_at: datetime
    last_seen_at: datetime
    ended_at: datetime | None = None


class OpsConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpsConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    admin_user_id: uuid.UUID
    ops_session_id: uuid.UUID | None = None
    state: OpsConversationState
    degraded: bool
    created_at: datetime
    updated_at: datetime


class OpsMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000)


class OpsMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: OpsMessageRole
    content: str = Field(min_length=1, max_length=4000)
    degraded: bool = False
    created_at: datetime


class OpsProposalSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID
    session_id: uuid.UUID
    action: ResolvedOpsAction
    rationale: str = Field(min_length=1, max_length=1000)


class OpsProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    conversation_id: uuid.UUID
    ops_session_id: uuid.UUID
    proposer: str
    action: ResolvedOpsAction
    rationale: str
    state: OpsProposalState
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    executed_at: datetime | None = None
    confirmed_by_user_id: uuid.UUID | None = None
    result: dict[str, object] | None = None
    failure_code: str | None = None


class OpsProposalIssued(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: OpsProposal
    confirm_token: str = Field(
        pattern=r"^coire_confirm_[A-Za-z0-9_-]{12}_[A-Za-z0-9_-]{43}$",
        repr=False,
    )


class OpsConversationDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation: OpsConversation
    messages: list[OpsMessage] = Field(default_factory=list)
    proposals: list[OpsProposal] = Field(default_factory=list)


class OpsConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_token: str = Field(
        pattern=r"^coire_confirm_[A-Za-z0-9_-]{12}_[A-Za-z0-9_-]{43}$",
        repr=False,
    )
    action: ResolvedOpsAction


class OpsDeclineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class OpsTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OpsTurnStatus
    answer: str = Field(min_length=1, max_length=4000)
    observed_at: datetime
    degraded: bool = False
    sources: list[str] = Field(default_factory=list)
    proposal: OpsProposalIssued | None = None
