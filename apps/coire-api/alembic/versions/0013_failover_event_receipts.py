"""Add idempotency receipts for reconciled Studio failover events.

Revision ID: 0013_failover_event_receipts
Revises: 0012_ops_confirmations
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_failover_event_receipts"
down_revision: str | None = "0012_ops_confirmations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "failover_event_receipts",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("host", sa.String(32), nullable=False),
        sa.Column("term", sa.BigInteger(), nullable=False),
        sa.Column(
            "audit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("audit_log.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reconciled_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_failover_event_receipts_host", "failover_event_receipts", ["host"])


def downgrade() -> None:
    op.drop_index("ix_failover_event_receipts_host", table_name="failover_event_receipts")
    op.drop_table("failover_event_receipts")
