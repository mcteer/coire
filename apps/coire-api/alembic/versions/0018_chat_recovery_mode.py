"""Distinguish explicit retry from continuation on chat turns.

Revision ID: 0018_chat_recovery_mode
Revises: 0017_chat_message_context
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_chat_recovery_mode"
down_revision: str | None = "0017_chat_message_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_turns", sa.Column("recovery_mode", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_turns", "recovery_mode")
