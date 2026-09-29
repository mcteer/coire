"""Track completion of durable chat content purge.

Revision ID: 0016_chat_purge_marker
Revises: 0015_chat_conversations
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_chat_purge_marker"
down_revision: str | None = "0015_chat_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_conversations", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_chat_conversations_purge",
        "chat_conversations",
        ["deleted_at", "purged_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_conversations_purge", table_name="chat_conversations")
    op.drop_column("chat_conversations", "purged_at")
