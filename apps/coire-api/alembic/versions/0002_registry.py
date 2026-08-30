"""Create the model registry, acquisition jobs, engines, and the audit log.

Revision ID: 0002_registry
Revises: 0001_nodes
Create Date: 2026-08-30

Enums are created explicitly with `checkfirst` and referenced with `create_type=False`, the
same shape as 0001: without it `create_table` tries to create each type a second time and the
migration fails with DuplicateObjectError on any re-run.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_registry"
down_revision = "0001_nodes"
branch_labels = None
depends_on = None

MODEL_STATE = ("downloading", "replicating", "ready", "failed", "retired")
VISIBILITY = ("admin_only", "published")
COPY_ROLE = ("origin", "replica")
DOWNLOAD_STAGE = (
    "inspect",
    "pull",
    "verify_origin",
    "export",
    "import",
    "verify_replica",
    "done",
    "failed",
)
ENGINE_STATE = ("starting", "ready", "stopping", "stopped", "failed", "orphan")
AUDIT_OUTCOME = ("ok", "refused", "error")

_MODEL_STATE = postgresql.ENUM(*MODEL_STATE, name="model_state", create_type=False)
_VISIBILITY = postgresql.ENUM(*VISIBILITY, name="visibility", create_type=False)
_COPY_ROLE = postgresql.ENUM(*COPY_ROLE, name="copy_role", create_type=False)
_DOWNLOAD_STAGE = postgresql.ENUM(*DOWNLOAD_STAGE, name="download_stage", create_type=False)
_ENGINE_STATE = postgresql.ENUM(*ENGINE_STATE, name="engine_state", create_type=False)
_AUDIT_OUTCOME = postgresql.ENUM(*AUDIT_OUTCOME, name="audit_outcome", create_type=False)

_ALL = (
    ("model_state", MODEL_STATE),
    ("visibility", VISIBILITY),
    ("copy_role", COPY_ROLE),
    ("download_stage", DOWNLOAD_STAGE),
    ("engine_state", ENGINE_STATE),
    ("audit_outcome", AUDIT_OUTCOME),
)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in _ALL:
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "models",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("repo_id", sa.String(255), nullable=False, unique=True),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("state", _MODEL_STATE, nullable=False, server_default="downloading"),
        sa.Column("state_reason", sa.Text, nullable=True),
        sa.Column("visibility", _VISIBILITY, nullable=False, server_default="admin_only"),
        sa.Column("entitlement", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("tags", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("placement_policy", sa.String(64), nullable=False, server_default="single:auto"),
        sa.Column("precision", sa.String(32), nullable=False),
        sa.Column("weight_bytes", sa.BigInteger, nullable=False),
        sa.Column("total_bytes", sa.BigInteger, nullable=False),
        sa.Column("file_count", sa.Integer, nullable=False),
        sa.Column("memory_estimate_bytes", sa.BigInteger, nullable=False),
        sa.Column("idle_ttl_seconds", sa.Integer, nullable=True),
        sa.Column("context_window", sa.Integer, nullable=True),
        sa.Column("chat_template", sa.Text, nullable=True),
        sa.Column("capability_profile", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("manifest_sha256", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_models_repo_id", "models", ["repo_id"], unique=True)
    op.create_index("ix_models_slug", "models", ["slug"], unique=True)
    op.create_index("ix_models_state", "models", ["state"])

    op.create_table(
        "model_state_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_state", _MODEL_STATE, nullable=True),
        sa.Column("to_state", _MODEL_STATE, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_model_state_transitions_model_id", "model_state_transitions", ["model_id"])

    op.create_table(
        "model_copies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("manifest_sha256", sa.String(64), nullable=True),
        sa.Column("verified", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mismatched_paths", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("role", _COPY_ROLE, nullable=False),
        sa.UniqueConstraint("model_id", "node_id", name="uq_model_copies_model_node"),
    )
    op.create_index("ix_model_copies_model_id", "model_copies", ["model_id"])

    op.create_table(
        "download_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "origin_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id"),
            nullable=False,
        ),
        sa.Column(
            "replica_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id"),
            nullable=False,
        ),
        sa.Column("stage", _DOWNLOAD_STAGE, nullable=False),
        sa.Column("bytes_done", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("bytes_total", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("files_done", sa.Integer, nullable=False, server_default="0"),
        sa.Column("files_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("transfer_grant", sa.String(64), nullable=True),
        sa.Column("manifest", postgresql.JSONB, nullable=True),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_download_jobs_model_id", "download_jobs", ["model_id"])
    op.create_index("ix_download_jobs_stage", "download_jobs", ["stage"])

    op.create_table(
        "engine_processes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Nullable: an orphan engine matches no expectation and may match no model either.
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("port", sa.Integer, nullable=False),
        sa.Column("pid", sa.Integer, nullable=True),
        sa.Column("process_create_time", sa.Float, nullable=True),
        sa.Column("state", _ENGINE_STATE, nullable=False),
        sa.Column("state_reason", sa.Text, nullable=True),
        sa.Column("estimate_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("resident_bytes", sa.BigInteger, nullable=True),
        sa.Column("resident_delta_bytes", sa.BigInteger, nullable=True),
        sa.Column("cpu_percent", sa.Float, nullable=True),
        sa.Column("chat_template_sha256", sa.String(64), nullable=True),
        sa.Column("load_seconds", sa.Float, nullable=True),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_engine_processes_model_id", "engine_processes", ["model_id"])
    op.create_index("ix_engine_processes_node_id", "engine_processes", ["node_id"])
    op.create_index("ix_engine_processes_state", "engine_processes", ["state"])

    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(255), nullable=False),
        sa.Column("outcome", _AUDIT_OUTCOME, nullable=False),
        sa.Column("detail", postgresql.JSONB, nullable=False, server_default="{}"),
    )
    op.create_index("ix_audit_log_at_desc", "audit_log", ["at"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_target_id", "audit_log", ["target_id"])


def downgrade() -> None:
    for table in (
        "audit_log",
        "engine_processes",
        "download_jobs",
        "model_copies",
        "model_state_transitions",
        "models",
    ):
        op.drop_table(table)
    bind = op.get_bind()
    for name, values in reversed(_ALL):
        postgresql.ENUM(*values, name=name).drop(bind, checkfirst=True)
