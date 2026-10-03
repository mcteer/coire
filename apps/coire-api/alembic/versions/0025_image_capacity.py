"""Image quota counters, fenced execution leases and measured coexistence profiles.

Revision ID: 0025_image_capacity
Revises: 0024_image_assets
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025_image_capacity"
down_revision: str | None = "0024_image_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSON = postgresql.JSONB()
UTC = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "image_quotas",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("owner_user_id", UUID, sa.ForeignKey("users.id", ondelete="RESTRICT")),
        sa.Column("held_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("stored_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("pending_jobs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("day_bucket", sa.Date(), nullable=False),
        sa.Column("held_outputs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_outputs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "(scope = 'owner' AND owner_user_id IS NOT NULL) OR "
            "(scope = 'global' AND owner_user_id IS NULL)",
            name="ck_image_quota_scope",
        ),
        sa.CheckConstraint(
            "held_bytes >= 0 AND stored_bytes >= 0 AND pending_jobs >= 0 "
            "AND held_outputs >= 0 AND consumed_outputs >= 0",
            name="ck_image_quota_nonnegative",
        ),
    )
    op.create_index(
        "uq_image_quota_owner",
        "image_quotas",
        ["owner_user_id"],
        unique=True,
        postgresql_where=sa.text("scope = 'owner'"),
    )
    op.create_index(
        "uq_image_quota_global",
        "image_quotas",
        ["scope"],
        unique=True,
        postgresql_where=sa.text("scope = 'global'"),
    )
    op.create_table(
        "image_execution_leases",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("node_id", UUID, sa.ForeignKey("nodes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("job_id", sa.String(26), sa.ForeignKey("image_jobs.id", ondelete="RESTRICT")),
        sa.Column("request_id", UUID),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("heartbeat_at", UTC, nullable=False),
        sa.Column("expires_at", UTC, nullable=False),
        sa.Column("released_at", UTC),
        sa.Column("release_evidence", JSON),
        sa.Column("created_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "(mode = 'image' AND job_id IS NOT NULL AND request_id IS NULL) OR "
            "(mode = 'inference' AND job_id IS NULL AND request_id IS NOT NULL)",
            name="ck_image_execution_lease_subject",
        ),
        sa.CheckConstraint("fence >= 0", name="ck_image_execution_lease_fence"),
        sa.CheckConstraint(
            "released_at IS NULL OR release_evidence IS NOT NULL",
            name="ck_image_execution_lease_release",
        ),
    )
    op.create_index(
        "ix_image_execution_leases_node_expiry",
        "image_execution_leases",
        ["node_id", "expires_at"],
    )
    op.create_table(
        "image_coexistence_profiles",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("profile_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("node_id", UUID, sa.ForeignKey("nodes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("hardware_fingerprint", sa.String(64), nullable=False),
        sa.Column("runtime_fingerprint", sa.String(64), nullable=False),
        sa.Column("chat_variant_ids", JSON, nullable=False),
        sa.Column(
            "image_model_id", UUID, sa.ForeignKey("models.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("image_mode", sa.String(16), nullable=False),
        sa.Column("measured_bounds", JSON, nullable=False),
        sa.Column("benchmark_result", JSON, nullable=False),
        sa.Column("first_token_p95_ms", sa.Float(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("valid_until", UTC, nullable=False),
        sa.Column("created_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.Column("invalidated_at", UTC),
        sa.CheckConstraint("first_token_p95_ms >= 0", name="ck_image_coexistence_latency"),
    )
    op.create_index(
        "ix_image_coexistence_node_status", "image_coexistence_profiles", ["node_id", "status"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("image_coexistence_profiles", "image_execution_leases", "image_quotas"):
        if bind.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) is not None:
            raise RuntimeError("export or remove image capacity records before downgrading")
    op.drop_index("ix_image_coexistence_node_status", table_name="image_coexistence_profiles")
    op.drop_table("image_coexistence_profiles")
    op.drop_index("ix_image_execution_leases_node_expiry", table_name="image_execution_leases")
    op.drop_table("image_execution_leases")
    op.drop_index("uq_image_quota_global", table_name="image_quotas")
    op.drop_index("uq_image_quota_owner", table_name="image_quotas")
    op.drop_table("image_quotas")
