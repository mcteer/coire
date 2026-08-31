"""Add append-only gateway usage accounting.

Revision ID: 0003_gateway_usage
Revises: 0002_registry
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_gateway_usage"
down_revision = "0002_registry"
branch_labels = None
depends_on = None

PROTOCOL = ("openai", "anthropic")
OUTCOME = ("succeeded", "failed", "disconnected", "refused")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*PROTOCOL, name="gateway_protocol").create(bind, checkfirst=True)
    postgresql.ENUM(*OUTCOME, name="usage_outcome").create(bind, checkfirst=True)
    protocol = postgresql.ENUM(*PROTOCOL, name="gateway_protocol", create_type=False)
    outcome = postgresql.ENUM(*OUTCOME, name="usage_outcome", create_type=False)
    op.create_table(
        "usage_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_kind", sa.String(32), nullable=False),
        sa.Column("principal_subject", sa.String(255), nullable=True),
        sa.Column("requested_model_id", sa.String(255), nullable=False),
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "engine_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("engine_processes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("protocol", protocol, nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Float, nullable=False),
        sa.Column("outcome", outcome, nullable=False),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_records_request_id", "usage_records", ["request_id"], unique=True)
    op.create_index("ix_usage_records_model_started", "usage_records", ["model_id", "started_at"])
    op.create_index(
        "ix_usage_records_principal_started", "usage_records", ["principal_subject", "started_at"]
    )
    op.create_index("ix_usage_records_outcome", "usage_records", ["outcome"])


def downgrade() -> None:
    op.drop_table("usage_records")
    bind = op.get_bind()
    postgresql.ENUM(*OUTCOME, name="usage_outcome").drop(bind, checkfirst=True)
    postgresql.ENUM(*PROTOCOL, name="gateway_protocol").drop(bind, checkfirst=True)
