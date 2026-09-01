"""Harness scorecards and exact-variant verification.

Revision ID: 0010_harness_evaluations
Revises: 0009_identity
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_harness_evaluations"
down_revision: str | None = "0009_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM("passed", "failed", "infrastructure_error", name="evaluation_verdict").create(
        bind, checkfirst=True
    )
    verdict = postgresql.ENUM(
        "passed",
        "failed",
        "infrastructure_error",
        name="evaluation_verdict",
        create_type=False,
    )
    op.add_column(
        "model_variants",
        sa.Column("harness_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("model_variants", sa.Column("harness_verified_at", sa.DateTime(timezone=True)))
    op.create_table(
        "harness_evaluations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "variant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_variants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scores", postgresql.JSONB(), nullable=False),
        sa.Column("overall_score", sa.Float(), nullable=False),
        sa.Column("verdict", verdict, nullable=False),
        sa.Column("harness_version", sa.String(32), nullable=False),
        sa.Column("engine_version", sa.String(64), nullable=False),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("run_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_harness_evaluations_variant_id", "harness_evaluations", ["variant_id"])
    op.create_index("ix_harness_evaluations_verdict", "harness_evaluations", ["verdict"])


def downgrade() -> None:
    op.drop_index("ix_harness_evaluations_verdict", table_name="harness_evaluations")
    op.drop_index("ix_harness_evaluations_variant_id", table_name="harness_evaluations")
    op.drop_table("harness_evaluations")
    op.drop_column("model_variants", "harness_verified_at")
    op.drop_column("model_variants", "harness_verified")
    postgresql.ENUM(name="evaluation_verdict").drop(op.get_bind(), checkfirst=True)
