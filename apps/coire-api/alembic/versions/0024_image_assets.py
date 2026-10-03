"""Private image inputs, outputs, transfer receipts and download grants.

Revision ID: 0024_image_assets
Revises: 0023_image_jobs_presets
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_image_assets"
down_revision: str | None = "0023_image_jobs_presets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSON = postgresql.JSONB()
UTC = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_unique_constraint("uq_image_job_id_owner", "image_jobs", ["id", "owner_user_id"])
    op.create_table(
        "image_inputs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "owner_user_id", UUID, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("original_key", sa.String(128), nullable=False),
        sa.Column("original_bytes", sa.BigInteger(), nullable=False),
        sa.Column("original_sha256", sa.String(64)),
        sa.Column("normalized_key", sa.String(128)),
        sa.Column("normalized_bytes", sa.BigInteger()),
        sa.Column("normalized_sha256", sa.String(64)),
        sa.Column("normalized_width", sa.Integer()),
        sa.Column("normalized_height", sa.Integer()),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("recipe", JSON),
        sa.Column("processing_job_id", sa.String(26)),
        sa.Column("held_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("active_references", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", UTC),
        sa.Column("purged_at", UTC),
        sa.CheckConstraint(
            "original_bytes >= 0 AND held_bytes >= 0 AND active_references >= 0",
            name="ck_image_input_counts",
        ),
        sa.CheckConstraint(
            "state <> 'ready' OR (original_bytes > 0 AND original_sha256 IS NOT NULL)",
            name="ck_image_input_ready_original",
        ),
        sa.CheckConstraint(
            "(purpose = 'recipe' AND (state <> 'ready' OR recipe IS NOT NULL) "
            "AND normalized_key IS NULL AND normalized_bytes IS NULL "
            "AND normalized_sha256 IS NULL AND normalized_width IS NULL "
            "AND normalized_height IS NULL) OR "
            "(purpose IN ('init', 'mask', 'control') AND "
            "(state <> 'ready' OR (normalized_key IS NOT NULL "
            "AND normalized_bytes IS NOT NULL AND normalized_bytes > 0 "
            "AND normalized_sha256 IS NOT NULL AND normalized_width IS NOT NULL "
            "AND normalized_width > 0 AND normalized_height IS NOT NULL "
            "AND normalized_height > 0)))",
            name="ck_image_input_purpose_shape",
        ),
    )
    op.create_index(
        "ix_image_inputs_owner_created", "image_inputs", ["owner_user_id", "created_at", "id"]
    )
    op.create_table(
        "image_outputs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("job_id", sa.String(26), nullable=False),
        sa.Column("owner_user_id", UUID, nullable=False),
        sa.Column("output_index", sa.Integer(), nullable=False),
        sa.Column("blob_key", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("pixel_sha256", sa.String(64), nullable=False),
        sa.Column("recipe", JSON, nullable=False),
        sa.Column("content_tag", sa.String(16), nullable=False),
        sa.Column("classifier_provenance", JSON, nullable=False),
        sa.Column("entitlement_snapshot", JSON, nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="staged"),
        sa.Column("created_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", UTC),
        sa.Column("deleted_at", UTC),
        sa.Column("purged_at", UTC),
        sa.UniqueConstraint("job_id", "output_index", name="uq_image_output_job_index"),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_image_output_id_owner"),
        sa.ForeignKeyConstraint(
            ["job_id", "owner_user_id"],
            ["image_jobs.id", "image_jobs.owner_user_id"],
            name="fk_image_output_job_owner",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("output_index >= 0 AND output_index < 4", name="ck_image_output_index"),
        sa.CheckConstraint("size_bytes > 0", name="ck_image_output_size"),
    )
    op.create_index(
        "ix_image_outputs_owner_created", "image_outputs", ["owner_user_id", "created_at", "id"]
    )
    op.create_table(
        "image_transfers",
        sa.Column(
            "job_id",
            sa.String(26),
            sa.ForeignKey("image_jobs.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("attempt", sa.Integer(), primary_key=True),
        sa.Column("output_index", sa.Integer(), primary_key=True),
        sa.Column("expected_bytes", sa.BigInteger(), nullable=False),
        sa.Column("expected_sha256", sa.String(64), nullable=False),
        sa.Column("staging_key", sa.String(128), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("receipt", JSON),
        sa.Column("node_cleanup_ack_at", UTC),
        sa.Column("lease_expires_at", UTC, nullable=False),
        sa.Column("grant_hash", sa.String(64), nullable=False),
        sa.Column("created_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "attempt >= 1 AND output_index >= 0 AND output_index < 4", name="ck_image_transfer_slot"
        ),
        sa.CheckConstraint("expected_bytes > 0", name="ck_image_transfer_size"),
    )
    op.create_index("ix_image_transfers_expiry", "image_transfers", ["lease_expires_at"])
    op.create_table(
        "image_download_grants",
        sa.Column("grant_hash", sa.String(64), primary_key=True),
        sa.Column("output_id", UUID, nullable=False),
        sa.Column("owner_user_id", UUID, nullable=False),
        sa.Column("access_policy_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", UTC, nullable=False),
        sa.Column("revoked_at", UTC),
        sa.Column("created_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["output_id", "owner_user_id"],
            ["image_outputs.id", "image_outputs.owner_user_id"],
            name="fk_image_grant_output_owner",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_image_download_grants_expiry", "image_download_grants", ["expires_at"])


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("image_download_grants", "image_transfers", "image_outputs", "image_inputs"):
        if bind.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) is not None:
            raise RuntimeError("export or remove image asset records before downgrading")
    op.drop_index("ix_image_download_grants_expiry", table_name="image_download_grants")
    op.drop_table("image_download_grants")
    op.drop_index("ix_image_transfers_expiry", table_name="image_transfers")
    op.drop_table("image_transfers")
    op.drop_index("ix_image_outputs_owner_created", table_name="image_outputs")
    op.drop_table("image_outputs")
    op.drop_index("ix_image_inputs_owner_created", table_name="image_inputs")
    op.drop_table("image_inputs")
    op.drop_constraint("uq_image_job_id_owner", "image_jobs", type_="unique")
