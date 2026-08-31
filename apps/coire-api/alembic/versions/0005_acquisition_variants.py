"""Add durable acquisition workflows and model variants.

Revision ID: 0005_acquisition_variants
Revises: 0004_merge_gateway_fabrics
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_acquisition_variants"
down_revision = "0004_merge_gateway_fabrics"
branch_labels = None
depends_on = None

ENUMS = {
    "variant_state": (
        "requested",
        "inspecting",
        "queued",
        "pulling",
        "converting",
        "validating",
        "replicating",
        "ready",
        "failed",
    ),
    "acquisition_stage": ("inspect", "pull", "convert", "validate", "replicate", "done", "failed"),
    "acquisition_state": (
        "queued",
        "running",
        "waiting_for_capacity",
        "succeeded",
        "failed",
        "cancelled",
    ),
    "acquisition_stage_status": ("pending", "running", "succeeded", "failed", "skipped"),
    "reservation_state": ("held", "released", "expired"),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "model_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False),
        sa.Column("precision", sa.String(16), nullable=False),
        sa.Column("recipe", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("byte_size", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("memory_estimate_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("estimate_delta_bytes", sa.BigInteger, nullable=True),
        sa.Column("state", _enum("variant_state"), nullable=False),
        sa.Column("validated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("published", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("raw_retained", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("model_id", "name", name="uq_model_variants_model_name"),
        sa.UniqueConstraint("slug", name="uq_model_variants_slug"),
    )
    op.create_index("ix_model_variants_model_id", "model_variants", ["model_id"])
    op.create_index("ix_model_variants_state", "model_variants", ["state"])

    op.execute(
        sa.text("""
        INSERT INTO model_variants
          (id, model_id, name, slug, source_revision, precision, recipe, byte_size,
           memory_estimate_bytes, state, validated, published, is_default, raw_retained)
        SELECT gen_random_uuid(), id, 'default', slug || '--default', 'legacy', precision,
               jsonb_build_object('name', 'default', 'precision', precision), total_bytes,
               memory_estimate_bytes,
               CASE WHEN state = 'ready' THEN 'ready'::variant_state ELSE 'failed'::variant_state END,
               (state = 'ready'), (visibility = 'published'), true, false
        FROM models
    """)
    )

    op.create_table(
        "acquisition_workflows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "variant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_variants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("repo_id", sa.String(255), nullable=False),
        sa.Column("revision", sa.String(128), nullable=False),
        sa.Column("request", postgresql.JSONB, nullable=False),
        sa.Column("keep_raw", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "origin_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id"),
            nullable=True,
        ),
        sa.Column(
            "replica_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id"),
            nullable=True,
        ),
        sa.Column("stage", _enum("acquisition_stage"), nullable=False),
        sa.Column("state", _enum("acquisition_state"), nullable=False),
        sa.Column("progress_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("total_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("failure_detail", sa.Text, nullable=True),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_acquisition_workflows_model_id", "acquisition_workflows", ["model_id"])
    op.create_index("ix_acquisition_workflows_variant_id", "acquisition_workflows", ["variant_id"])
    op.create_index("ix_acquisition_workflows_stage", "acquisition_workflows", ["stage"])
    op.create_index("ix_acquisition_workflows_state", "acquisition_workflows", ["state"])

    op.create_table(
        "acquisition_stages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("acquisition_workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", _enum("acquisition_stage"), nullable=False),
        sa.Column("status", _enum("acquisition_stage_status"), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("result", postgresql.JSONB, nullable=True),
        sa.Column("result_digest", sa.String(64), nullable=True),
        sa.Column("public_summary", sa.Text, nullable=True),
        sa.Column("node_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("workflow_id", "stage", "attempt", name="uq_acquisition_stage_attempt"),
    )
    op.create_index("ix_acquisition_stages_workflow_id", "acquisition_stages", ["workflow_id"])

    op.create_table(
        "acquisition_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("acquisition_workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", _enum("acquisition_stage"), nullable=False),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("result", postgresql.JSONB, nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("failure_detail", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_acquisition_commands_workflow_id", "acquisition_commands", ["workflow_id"])
    op.create_index("ix_acquisition_commands_state", "acquisition_commands", ["state"])

    op.create_table(
        "inspection_results",
        sa.Column(
            "workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("acquisition_workflows.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("result", postgresql.JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "validation_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("acquisition_workflows.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "variant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_variants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB, nullable=False),
        sa.Column("validated", sa.Boolean, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_validation_results_variant_id", "validation_results", ["variant_id"])
    op.create_table(
        "variant_copies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "variant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_variants.id", ondelete="CASCADE"),
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
        sa.Column(
            "role",
            postgresql.ENUM("origin", "replica", name="copy_role", create_type=False),
            nullable=False,
        ),
        sa.UniqueConstraint("variant_id", "node_id", name="uq_variant_copies_variant_node"),
    )
    op.create_index("ix_variant_copies_variant_id", "variant_copies", ["variant_id"])

    op.execute(
        sa.text("""
        INSERT INTO variant_copies
          (id, variant_id, node_id, path, bytes, manifest_sha256, verified, verified_at, role)
        SELECT gen_random_uuid(), v.id, c.node_id, c.path, c.bytes, c.manifest_sha256,
               c.verified, c.verified_at, c.role
        FROM model_copies c JOIN model_variants v ON v.model_id = c.model_id AND v.is_default
    """)
    )

    op.create_table(
        "node_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("acquisition_workflows.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "variant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_variants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("memory_bytes", sa.BigInteger, nullable=False),
        sa.Column("disk_bytes", sa.BigInteger, nullable=False),
        sa.Column("state", _enum("reservation_state"), nullable=False),
        sa.Column("occupants", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_node_reservations_workflow_id", "node_reservations", ["workflow_id"])


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(
        sa.text("SELECT count(*) FROM model_variants WHERE name <> 'default'")
    ).scalar_one()
    if count:
        raise RuntimeError("cannot downgrade after creating additional model variants")
    for table in (
        "node_reservations",
        "variant_copies",
        "validation_results",
        "inspection_results",
        "acquisition_commands",
        "acquisition_stages",
        "acquisition_workflows",
        "model_variants",
    ):
        op.drop_table(table)
    for name, values in reversed(tuple(ENUMS.items())):
        postgresql.ENUM(*values, name=name).drop(bind, checkfirst=True)
