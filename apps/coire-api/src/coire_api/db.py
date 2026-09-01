"""Database engine and session management.

`pool_pre_ping` and a short connect timeout are what let a restarting Postgres produce a fast,
honest `unhealthy` and then recover without restarting the API (spec US2 scenario 2).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from coire_core.models.acquisition import (
    AcquisitionStage,
    AcquisitionState,
    ReservationState,
    StageStatus,
    VariantState,
)
from coire_core.models.audit import AuditOutcome
from coire_core.models.auth import ActorType, UserRole
from coire_core.models.engine import EngineState
from coire_core.models.gateway import GatewayProtocol, UsageOutcome
from coire_core.models.harness import EvaluationVerdict
from coire_core.models.instance import InstanceState
from coire_core.models.jobs import DownloadStage
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.ops import (
    OpsConversationState,
    OpsMessageRole,
    OpsProposalState,
    OpsSessionState,
)
from coire_core.models.placement import (
    MemoryReservationState,
    PlacementState,
    ReservationHolder,
)
from coire_core.models.registry import CopyRole, ModelState, Visibility
from coire_core.models.runs import AgentRunState, RunCommandState, RunOperation
from coire_core.models.sharding import (
    BenchmarkRunState,
    ProbeOutcome,
    ProbeTransport,
    ShardGroupState,
    ShardingMode,
)
from coire_core.settings import Settings


def _enum(python_enum: type, name: str) -> SAEnum:
    """A Postgres enum whose labels are the string values, not the member names.

    Without `values_callable` SQLAlchemy stores `DOWNLOADING` where every other consumer —
    the API, the contract, psql — says `downloading`.
    """
    return SAEnum(python_enum, name=name, values_callable=lambda e: [m.value for m in e])


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


class Base(DeclarativeBase):
    pass


class NodeRow(Base):
    """The only persisted entity in feature 000 (data-model.md)."""

    __tablename__ = "nodes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[NodeRole] = mapped_column(
        SAEnum(NodeRole, name="node_role", values_callable=lambda e: [m.value for m in e])
    )
    mesh_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    egress_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    endpoint_contract_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    control_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    data_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 256 GB of RAM and 1.8 TB of disk both overflow a 32-bit column.
    memory_total_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_total_bytes: Mapped[int] = mapped_column(BigInteger)
    gpu_cores: Mapped[int | None] = mapped_column(nullable=True)
    agent_version: Mapped[str] = mapped_column(String(32))
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reachability: Mapped[Reachability] = mapped_column(
        SAEnum(Reachability, name="reachability", values_callable=lambda e: [m.value for m in e]),
        default=Reachability.UNKNOWN,
    )
    probe_failures: Mapped[int] = mapped_column(default=0)
    declared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    registration_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    token_revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    health_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    gpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine. Kept separate from `init_engine` so tests can make their own."""
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=5,
        connect_args={"timeout": 5.0, "command_timeout": 10.0},
    )


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_engine(settings)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A session that commits on the way out, for work that is not a route's own transaction.

    The refusal audit row uses this: the request is being abandoned, so there is no route
    transaction for the row to join.
    """
    if _sessionmaker is None:
        raise RuntimeError("engine not initialised; call init_engine() during startup")
    async with _sessionmaker() as session:
        yield session
        await session.commit()


async def get_session() -> AsyncIterator[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("engine not initialised; call init_engine() during startup")
    async with _sessionmaker() as session:
        yield session


# --------------------------------------------------------------------------- feature 001
#
# Everything below is the registry. It lives on core, in Postgres, because nothing on a Studio
# may be a source of truth (Principle II): the agents keep caches under /opt/coire/state and
# the reconciler corrects them against these rows.


class ModelRow(Base):
    """The registry record (spec FR-001)."""

    __tablename__ = "models"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    state: Mapped[ModelState] = mapped_column(
        _enum(ModelState, "model_state"), index=True, default=ModelState.DOWNLOADING
    )
    state_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    visibility: Mapped[Visibility] = mapped_column(
        _enum(Visibility, "visibility"), default=Visibility.ADMIN_ONLY
    )
    entitlement: Mapped[list[str]] = mapped_column(JSONB, default=list)
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    placement_policy: Mapped[str] = mapped_column(String(64), default="single:auto")

    precision: Mapped[str] = mapped_column(String(32))
    # A 1.8 TB model is not plausible today, but 32-bit columns for byte counts are how you
    # find out; every size here is BigInteger.
    weight_bytes: Mapped[int] = mapped_column(BigInteger)
    total_bytes: Mapped[int] = mapped_column(BigInteger)
    file_count: Mapped[int] = mapped_column(Integer)
    memory_estimate_bytes: Mapped[int] = mapped_column(BigInteger)

    idle_ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chat_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    capability_profile: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelStateTransitionRow(Base):
    """Append-only history of every state change (spec FR-002)."""

    __tablename__ = "model_state_transitions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), index=True
    )
    from_state: Mapped[ModelState | None] = mapped_column(
        _enum(ModelState, "model_state"), nullable=True
    )
    to_state: Mapped[ModelState] = mapped_column(_enum(ModelState, "model_state"))
    reason: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelCopyRow(Base):
    """One model's presence on one node. `ready` means two of these verify (spec FR-008)."""

    __tablename__ = "model_copies"
    __table_args__ = (UniqueConstraint("model_id", "node_id", name="uq_model_copies_model_node"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String(512))
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified: Mapped[bool] = mapped_column(default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mismatched_paths: Mapped[list[str]] = mapped_column(JSONB, default=list)
    role: Mapped[CopyRole] = mapped_column(_enum(CopyRole, "copy_role"))


class DownloadJobRow(Base):
    """The acquisition cursor (ADR-0005). Its id is the job id every node verb is idempotent
    on, which is what lets a restarted control plane re-issue the current stage safely."""

    __tablename__ = "download_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), index=True
    )
    origin_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id"))
    replica_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id"))
    stage: Mapped[DownloadStage] = mapped_column(_enum(DownloadStage, "download_stage"), index=True)
    bytes_done: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_total: Mapped[int] = mapped_column(BigInteger, default=0)
    files_done: Mapped[int] = mapped_column(Integer, default=0)
    files_total: Mapped[int] = mapped_column(Integer, default=0)
    transfer_grant: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    """The origin's manifest, carried here so the import request can hand it to the replica
    without a second round trip to the origin."""
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- feature 002


class ModelVariantRow(Base):
    __tablename__ = "model_variants"
    __table_args__ = (
        UniqueConstraint("model_id", "name", name="uq_model_variants_model_name"),
        UniqueConstraint("slug", name="uq_model_variants_slug"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    slug: Mapped[str] = mapped_column(String(255))
    source_revision: Mapped[str] = mapped_column(String(128))
    precision: Mapped[str] = mapped_column(String(16))
    recipe: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    byte_size: Mapped[int] = mapped_column(BigInteger, default=0)
    memory_estimate_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    estimate_delta_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    state: Mapped[VariantState] = mapped_column(_enum(VariantState, "variant_state"), index=True)
    validated: Mapped[bool] = mapped_column(Boolean, default=False)
    harness_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    harness_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_retained: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HarnessEvaluationRow(Base):
    __tablename__ = "harness_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_variants.id", ondelete="CASCADE"), index=True
    )
    scores: Mapped[dict[str, object]] = mapped_column(JSONB)
    overall_score: Mapped[float] = mapped_column(Float)
    verdict: Mapped[EvaluationVerdict] = mapped_column(
        _enum(EvaluationVerdict, "evaluation_verdict"), index=True
    )
    harness_version: Mapped[str] = mapped_column(String(32))
    engine_version: Mapped[str] = mapped_column(String(64))
    diagnostics: Mapped[list[str]] = mapped_column(JSONB, default=list)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- feature 011


class AgentRunRow(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    requester_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    profile: Mapped[str] = mapped_column(String(16))
    primary_model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="RESTRICT"), index=True
    )
    primary_variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_variants.id", ondelete="RESTRICT"), index=True
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    workspace_ref: Mapped[str] = mapped_column(String(128))
    token_scope: Mapped[dict[str, object]] = mapped_column(JSONB)
    state: Mapped[AgentRunState] = mapped_column(
        _enum(AgentRunState, "agent_run_state"), index=True
    )
    limits: Mapped[dict[str, object]] = mapped_column(JSONB)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    resource_usage: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    killed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    killed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRunTransitionRow(Base):
    __tablename__ = "agent_run_transitions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    from_state: Mapped[AgentRunState | None] = mapped_column(
        _enum(AgentRunState, "agent_run_state"), nullable=True
    )
    to_state: Mapped[AgentRunState] = mapped_column(_enum(AgentRunState, "agent_run_state"))
    reason: Mapped[str] = mapped_column(String(500))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RunTokenRow(Base):
    __tablename__ = "run_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), unique=True, index=True
    )
    prefix: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(255))
    scope: Mapped[dict[str, object]] = mapped_column(JSONB)
    spent_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RunCommandRow(Base):
    __tablename__ = "run_commands"
    __table_args__ = (
        UniqueConstraint("run_id", "operation", "attempt", name="uq_run_command_attempt"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True
    )
    operation: Mapped[RunOperation] = mapped_column(_enum(RunOperation, "run_operation"))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[RunCommandState] = mapped_column(
        _enum(RunCommandState, "run_command_state"), index=True
    )
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AcquisitionWorkflowRow(Base):
    __tablename__ = "acquisition_workflows"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_variants.id", ondelete="CASCADE"), index=True
    )
    repo_id: Mapped[str] = mapped_column(String(255))
    revision: Mapped[str] = mapped_column(String(128))
    request: Mapped[dict[str, object]] = mapped_column(JSONB)
    keep_raw: Mapped[bool] = mapped_column(Boolean, default=False)
    origin_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    replica_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    stage: Mapped[AcquisitionStage] = mapped_column(
        _enum(AcquisitionStage, "acquisition_stage"), index=True
    )
    state: Mapped[AcquisitionState] = mapped_column(
        _enum(AcquisitionState, "acquisition_state"), index=True
    )
    progress_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AcquisitionStageRow(Base):
    __tablename__ = "acquisition_stages"
    __table_args__ = (
        UniqueConstraint("workflow_id", "stage", "attempt", name="uq_acquisition_stage_attempt"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("acquisition_workflows.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[AcquisitionStage] = mapped_column(_enum(AcquisitionStage, "acquisition_stage"))
    status: Mapped[StageStatus] = mapped_column(_enum(StageStatus, "acquisition_stage_status"))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    result_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    public_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    node_job_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AcquisitionCommandRow(Base):
    """Scheduler-to-API handoff; only the API is authorised to reach node agents."""

    __tablename__ = "acquisition_commands"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("acquisition_workflows.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[AcquisitionStage] = mapped_column(_enum(AcquisitionStage, "acquisition_stage"))
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    operation: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InspectionResultRow(Base):
    __tablename__ = "inspection_results"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("acquisition_workflows.id", ondelete="CASCADE"), primary_key=True
    )
    result: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ValidationResultRow(Base):
    __tablename__ = "validation_results"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("acquisition_workflows.id", ondelete="CASCADE"), unique=True
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_variants.id", ondelete="CASCADE"), index=True
    )
    result: Mapped[dict[str, object]] = mapped_column(JSONB)
    validated: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VariantCopyRow(Base):
    __tablename__ = "variant_copies"
    __table_args__ = (
        UniqueConstraint("variant_id", "node_id", name="uq_variant_copies_variant_node"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_variants.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String(512))
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    role: Mapped[CopyRole] = mapped_column(_enum(CopyRole, "copy_role"))


class NodeReservationRow(Base):
    __tablename__ = "node_reservations"
    __table_args__ = (UniqueConstraint("workflow_id", name="uq_node_reservations_workflow"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("acquisition_workflows.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_variants.id", ondelete="CASCADE")
    )
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    memory_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_bytes: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[ReservationState] = mapped_column(_enum(ReservationState, "reservation_state"))
    occupants: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- feature 004


class NodeMemoryLedgerRow(Base):
    __tablename__ = "node_memory_ledgers"

    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    budget_bytes: Mapped[int] = mapped_column(BigInteger)
    sandbox_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    measured_resident_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    thermal_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    health: Mapped[Reachability] = mapped_column(_enum(Reachability, "reachability"))
    health_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    health_sampled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryReservationRow(Base):
    __tablename__ = "memory_reservations"
    __table_args__ = (
        UniqueConstraint(
            "node_id", "holder_type", "holder_id", name="uq_memory_reservation_holder"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    holder_type: Mapped[ReservationHolder] = mapped_column(
        _enum(ReservationHolder, "reservation_holder")
    )
    holder_id: Mapped[str] = mapped_column(String(255))
    bytes: Mapped[int] = mapped_column(BigInteger)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[MemoryReservationState] = mapped_column(
        _enum(MemoryReservationState, "memory_reservation_state"), index=True
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RequestLeaseRow(Base):
    __tablename__ = "request_leases"
    __table_args__ = (UniqueConstraint("request_id", name="uq_request_lease_request"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memory_reservations.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlacementDecisionRow(Base):
    __tablename__ = "placement_decisions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_variants.id", ondelete="CASCADE"), index=True
    )
    policy: Mapped[str] = mapped_column(String(64))
    required_bytes: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[PlacementState] = mapped_column(
        _enum(PlacementState, "placement_state"), index=True
    )
    selected_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("nodes.id"), nullable=True
    )
    evicted_reservation_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    refusal_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refusal_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    occupants: Mapped[list[dict[str, object]]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvictionEventRow(Base):
    __tablename__ = "eviction_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("placement_decisions.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id"))
    reservation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memory_reservations.id"))
    lru_rank: Mapped[int] = mapped_column(Integer)
    skipped: Mapped[list[dict[str, object]]] = mapped_column(JSONB, default=list)
    outcome: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlacementCommandRow(Base):
    """Durable scheduler-to-API engine command; scheduler never holds node credentials."""

    __tablename__ = "placement_commands"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("placement_decisions.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memory_reservations.id", ondelete="SET NULL"), nullable=True
    )
    engine_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    operation: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    state: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- feature 005


class ModelInstanceRow(Base):
    __tablename__ = "model_instances"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_variants.id", ondelete="CASCADE"), index=True
    )
    placement_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("placement_decisions.id", ondelete="SET NULL"), nullable=True
    )
    policy: Mapped[str] = mapped_column(String(64))
    state: Mapped[InstanceState] = mapped_column(_enum(InstanceState, "instance_state"), index=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    in_flight: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    transitioned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    drain_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fallback_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fallback_instance_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    fallback_no_fit: Mapped[bool] = mapped_column(Boolean, default=False)


class InstanceMemberRow(Base):
    __tablename__ = "instance_members"
    __table_args__ = (
        UniqueConstraint("instance_id", "rank", name="uq_instance_member_rank"),
        UniqueConstraint("instance_id", "node_id", name="uq_instance_member_node"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_instances.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    rank: Mapped[int] = mapped_column(Integer)
    engine_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memory_reservations.id", ondelete="SET NULL"), nullable=True
    )
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rank_healthy: Mapped[bool] = mapped_column(Boolean, default=False)
    last_rank_health_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LinkObservationRow(Base):
    __tablename__ = "link_observations"
    __table_args__ = (
        Index("ix_link_observations_pair_at", "node_a_id", "node_b_id", "observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    node_a_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    node_b_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    transport: Mapped[ProbeTransport] = mapped_column(_enum(ProbeTransport, "probe_transport"))
    outcome: Mapped[ProbeOutcome] = mapped_column(_enum(ProbeOutcome, "probe_outcome"))
    bandwidth_bytes_per_second: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    os_version_a: Mapped[str] = mapped_column(String(64))
    os_version_b: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ShardGroupRow(Base):
    __tablename__ = "shard_groups"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_instances.id", ondelete="CASCADE"), unique=True, index=True
    )
    mode: Mapped[ShardingMode] = mapped_column(_enum(ShardingMode, "sharding_mode"))
    state: Mapped[ShardGroupState] = mapped_column(_enum(ShardGroupState, "shard_group_state"))
    command_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    hostfile_sha256: Mapped[str] = mapped_column(String(64))
    state_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ShardCommandRow(Base):
    """Durable scheduler-to-API command; node credentials remain in coire-api."""

    __tablename__ = "shard_commands"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shard_groups.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    operation: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    state: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlacementBenchmarkRow(Base):
    __tablename__ = "placement_benchmarks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_runs.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_variants.id", ondelete="CASCADE"), index=True
    )
    placement: Mapped[str] = mapped_column(String(64))
    tokens_per_second: Mapped[float | None] = mapped_column(Float, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer)
    generation_tokens: Mapped[int] = mapped_column(Integer)
    gpu_cores: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)
    os_versions: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    engine_version: Mapped[str] = mapped_column(String(64))
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BenchmarkRunRow(Base):
    __tablename__ = "benchmark_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_variants.id", ondelete="CASCADE"), index=True
    )
    state: Mapped[BenchmarkRunState] = mapped_column(
        _enum(BenchmarkRunState, "benchmark_run_state"), index=True
    )
    prompt_tokens: Mapped[int] = mapped_column(Integer)
    generation_tokens: Mapped[int] = mapped_column(Integer)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BenchmarkCommandRow(Base):
    __tablename__ = "benchmark_commands"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_runs.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InstanceTransitionRow(Base):
    __tablename__ = "instance_transitions"
    __table_args__ = (
        UniqueConstraint("instance_id", "sequence", name="uq_instance_transition_sequence"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_instances.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    previous_state: Mapped[InstanceState | None] = mapped_column(
        _enum(InstanceState, "instance_state"), nullable=True
    )
    state: Mapped[InstanceState] = mapped_column(_enum(InstanceState, "instance_state"))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RegistrationAttemptRow(Base):
    __tablename__ = "registration_attempts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True
    )
    node_name: Mapped[str] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(64))
    agent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    remote_identity: Mapped[str | None] = mapped_column(String(255), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EngineProcessRow(Base):
    """A running engine. Feature 005 generalises this into ModelInstance."""

    __tablename__ = "engine_processes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    instance_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_instances.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Nullable: an orphan is a running engine that matches no expectation, and it may not
    # correspond to any model this registry knows (spec FR-015).
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), nullable=True, index=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    port: Mapped[int] = mapped_column(Integer)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    process_create_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Half of the process identity. A pid alone is reused; this is what makes adoption after
    an agent restart safe (spec FR-015, research R4)."""
    state: Mapped[EngineState] = mapped_column(_enum(EngineState, "engine_state"), index=True)
    state_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimate_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    resident_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resident_delta_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    chat_template_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    load_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    """How long this load took. The first measurement a cold model's warm-up estimate uses."""
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[UserRole] = mapped_column(_enum(UserRole, "user_role"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EntitlementRow(Base):
    __tablename__ = "entitlements"
    __table_args__ = (Index("ix_entitlements_user_name", "user_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    granted_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiKeyRow(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(12), index=True)
    secret_hash: Mapped[str] = mapped_column(Text)
    credential_version: Mapped[int] = mapped_column(Integer, default=1)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    requests_per_minute: Mapped[int] = mapped_column(Integer)
    monthly_budget_tokens: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RateWindowRow(Base):
    __tablename__ = "api_key_rate_windows"

    api_key_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requests: Mapped[int] = mapped_column(Integer, default=0)


class UsageAccumulatorRow(Base):
    __tablename__ = "api_key_usage_accumulators"

    api_key_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requests: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0)


class AuditRow(Base):
    """Append-only. No route in this application deletes or modifies one (feature 007
    FR-018 is honoured from the first row)."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_at_desc", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(128))
    actor_type: Mapped[ActorType] = mapped_column(
        _enum(ActorType, "audit_actor_type"), default=ActorType.SERVICE
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    request_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(255), index=True)
    outcome: Mapped[AuditOutcome] = mapped_column(_enum(AuditOutcome, "audit_outcome"))
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    before: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    after: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    context: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)


class OpsSessionRow(Base):
    __tablename__ = "ops_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    service_instance: Mapped[str] = mapped_column(String(128))
    state: Mapped[OpsSessionState] = mapped_column(
        _enum(OpsSessionState, "ops_session_state"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OpsConversationRow(Base):
    __tablename__ = "ops_conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    ops_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ops_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    state: Mapped[OpsConversationState] = mapped_column(
        _enum(OpsConversationState, "ops_conversation_state"), index=True
    )
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OpsMessageRow(Base):
    __tablename__ = "ops_messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[OpsMessageRole] = mapped_column(_enum(OpsMessageRole, "ops_message_role"))
    content: Mapped[str] = mapped_column(String(4000))
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OpsProposalRow(Base):
    __tablename__ = "ops_proposals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_conversations.id", ondelete="CASCADE"), index=True
    )
    ops_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_sessions.id", ondelete="RESTRICT"), index=True
    )
    proposer: Mapped[str] = mapped_column(String(128))
    action: Mapped[dict[str, object]] = mapped_column(JSONB)
    action_digest: Mapped[str] = mapped_column(String(64))
    rationale: Mapped[str] = mapped_column(String(1000))
    state: Mapped[OpsProposalState] = mapped_column(
        _enum(OpsProposalState, "ops_proposal_state"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class OpsConfirmationTokenRow(Base):
    __tablename__ = "ops_confirmation_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_proposals.id", ondelete="CASCADE"), unique=True, index=True
    )
    prefix: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(255))
    action_digest: Mapped[str] = mapped_column(String(64))
    issued_to_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsageRecordRow(Base):
    """Append-only accounting for every gateway request that reaches resolution."""

    __tablename__ = "usage_records"
    __table_args__ = (
        Index("ix_usage_records_model_started", "model_id", "started_at"),
        Index("ix_usage_records_principal_started", "principal_subject", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(unique=True, index=True)
    principal_kind: Mapped[str] = mapped_column(String(32))
    principal_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_model_id: Mapped[str] = mapped_column(String(255))
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("models.id", ondelete="SET NULL"), nullable=True
    )
    engine_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("engine_processes.id", ondelete="SET NULL"), nullable=True
    )
    protocol: Mapped[GatewayProtocol] = mapped_column(_enum(GatewayProtocol, "gateway_protocol"))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[float] = mapped_column(Float)
    outcome: Mapped[UsageOutcome] = mapped_column(_enum(UsageOutcome, "usage_outcome"), index=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
