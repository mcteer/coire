"""Persist selected file modes and the exact bounded prompt snapshot.

Revision ID: 0017_chat_message_context
Revises: 0016_chat_purge_marker
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_chat_message_context"
down_revision: str | None = "0016_chat_purge_marker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column(
            "attachment_selections",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("chat_messages", sa.Column("prompt_content", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "prompt_content")
    op.drop_column("chat_messages", "attachment_selections")
