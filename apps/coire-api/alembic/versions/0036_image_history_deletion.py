"""Hide deleted image history while retaining published output provenance."""

import sqlalchemy as sa
from alembic import op

revision = "0036_image_history_deletion"
down_revision = "0035_local_browser_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "image_jobs", sa.Column("history_deleted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("image_jobs", "history_deleted_at")
