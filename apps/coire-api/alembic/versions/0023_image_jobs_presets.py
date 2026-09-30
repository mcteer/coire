"""Private image jobs, events and append-only preset revisions.

Revision ID: 0023_image_jobs_presets
Revises: 0022_stopped_usage_outcome
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_image_jobs_presets"
down_revision: str | None = "0022_stopped_usage_outcome"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSON = postgresql.JSONB()
UTC = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "image_presets",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("description", sa.String(500), nullable=False, server_default=""),
        sa.Column("current_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(16), nullable=False, server_default="draft"),
        sa.Column(
            "created_by_user_id",
            UUID,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("current_revision >= 1", name="ck_image_preset_revision"),
    )
    op.create_index("ix_image_presets_state_name", "image_presets", ["state", "name"])
    op.create_table(
        "image_preset_revisions",
        sa.Column(
            "preset_id",
            UUID,
            sa.ForeignKey("image_presets.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("defaults", JSON, nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False, server_default=""),
        sa.Column("dependency_ids", JSON, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "entitlement_requirements", JSON, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "created_by_user_id",
            UUID,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("revision >= 1", name="ck_image_preset_revision_number"),
    )
    op.execute(
        "CREATE FUNCTION reject_image_preset_revision_change() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'image preset revisions are immutable'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER image_preset_revision_immutable "
        "BEFORE UPDATE OR DELETE ON image_preset_revisions "
        "FOR EACH ROW EXECUTE FUNCTION reject_image_preset_revision_change()"
    )
    op.create_table(
        "image_jobs",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column(
            "owner_user_id", UUID, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("originating_key_id", UUID, sa.ForeignKey("api_keys.id", ondelete="SET NULL")),
        sa.Column("originating_key_version", sa.Integer()),
        sa.Column("browser_identity", sa.String(128)),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("intent_sha256", sa.String(64), nullable=False),
        sa.Column("submitted_spec", JSON, nullable=False),
        sa.Column("resolved_spec", JSON, nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("fence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("workflow_id", sa.String(128)),
        sa.Column("preset_id", UUID),
        sa.Column("preset_revision", sa.Integer()),
        sa.Column("selected_node_id", UUID, sa.ForeignKey("nodes.id", ondelete="SET NULL")),
        sa.Column("instance_id", UUID, sa.ForeignKey("model_instances.id", ondelete="SET NULL")),
        sa.Column("reservation_ids", JSON, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("queued_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.Column("deadline_at", UTC, nullable=False),
        sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cancel_requested_at", UTC),
        sa.Column("safe_failure_code", sa.String(64)),
        sa.Column("receipt_state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("cleanup_state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("authorization_snapshot", JSON, nullable=False),
        sa.Column("created_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", UTC),
        sa.UniqueConstraint("owner_user_id", "idempotency_key", name="uq_image_job_owner_key"),
        sa.ForeignKeyConstraint(
            ["preset_id", "preset_revision"],
            ["image_preset_revisions.preset_id", "image_preset_revisions.revision"],
            name="fk_image_job_preset_revision",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("id ~ '^[0-9A-HJKMNP-TV-Z]{26}$'", name="ck_image_job_ulid"),
        sa.CheckConstraint(
            "version >= 1 AND attempt >= 1 AND fence >= 0", name="ck_image_job_versions"
        ),
        sa.CheckConstraint("progress >= 0 AND progress <= 1", name="ck_image_job_progress"),
        sa.CheckConstraint(
            "(preset_id IS NULL) = (preset_revision IS NULL)",
            name="ck_image_job_preset_pair",
        ),
    )
    op.create_index(
        "ix_image_jobs_owner_created", "image_jobs", ["owner_user_id", "created_at", "id"]
    )
    op.create_index("ix_image_jobs_state_deadline", "image_jobs", ["state", "deadline_at"])
    op.create_table(
        "image_job_events",
        sa.Column(
            "job_id",
            sa.String(26),
            sa.ForeignKey("image_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload", JSON, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", UTC, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("sequence >= 1", name="ck_image_job_event_sequence"),
    )
    op.create_index("ix_image_job_events_created", "image_job_events", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("image_job_events", "image_jobs", "image_preset_revisions", "image_presets"):
        occupied = bind.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
        if occupied is not None:
            raise RuntimeError("export or remove image records before downgrading")
    op.drop_index("ix_image_job_events_created", table_name="image_job_events")
    op.drop_table("image_job_events")
    op.drop_index("ix_image_jobs_state_deadline", table_name="image_jobs")
    op.drop_index("ix_image_jobs_owner_created", table_name="image_jobs")
    op.drop_table("image_jobs")
    op.execute("DROP TRIGGER image_preset_revision_immutable ON image_preset_revisions")
    op.drop_table("image_preset_revisions")
    op.execute("DROP FUNCTION reject_image_preset_revision_change()")
    op.drop_index("ix_image_presets_state_name", table_name="image_presets")
    op.drop_table("image_presets")
