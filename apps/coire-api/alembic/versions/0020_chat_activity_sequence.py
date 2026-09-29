"""Persist the last collected coding activity sequence per Chat turn.

Revision ID: 0020_chat_activity_sequence
Revises: 0019_engine_backend
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_chat_activity_sequence"
down_revision: str | None = "0019_engine_backend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_turns",
        sa.Column("activity_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("chat_turns", sa.Column("activity_final_state", sa.String(16), nullable=True))
    op.create_check_constraint(
        "ck_chat_turn_activity_sequence", "chat_turns", "activity_sequence >= 0"
    )
    op.create_index(
        "uq_chat_turn_run",
        "chat_turns",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_chat_turn_run", table_name="chat_turns")
    op.drop_constraint("ck_chat_turn_activity_sequence", "chat_turns", type_="check")
    op.drop_column("chat_turns", "activity_sequence")
    op.drop_column("chat_turns", "activity_final_state")
