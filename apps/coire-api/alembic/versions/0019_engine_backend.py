"""Record the owned backend for text and vision engine processes.

Revision ID: 0019_engine_backend
Revises: 0018_chat_recovery_mode
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_engine_backend"
down_revision: str | None = "0018_chat_recovery_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "engine_processes",
        sa.Column("backend", sa.String(16), nullable=False, server_default="mlx_lm"),
    )


def downgrade() -> None:
    op.drop_column("engine_processes", "backend")
