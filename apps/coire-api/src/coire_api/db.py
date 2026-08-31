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
from sqlalchemy.dialects.postgresql import INET, JSONB
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
from coire_core.models.engine import EngineState
from coire_core.models.gateway import GatewayProtocol, UsageOutcome
from coire_core.models.jobs import DownloadStage
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.registry import CopyRole, ModelState, Visibility
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
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_retained: Mapped[bool] = mapped_column(Boolean, default=False)
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


class EngineProcessRow(Base):
    """A running engine. Feature 005 generalises this into ModelInstance."""

    __tablename__ = "engine_processes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
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


class AuditRow(Base):
    """Append-only. No route in this application deletes or modifies one (feature 007
    FR-018 is honoured from the first row)."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_at_desc", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(255), index=True)
    outcome: Mapped[AuditOutcome] = mapped_column(_enum(AuditOutcome, "audit_outcome"))
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)


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
