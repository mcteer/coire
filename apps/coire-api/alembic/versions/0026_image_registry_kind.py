"""Persist registry asset kind with backend/source consistency.

Revision ID: 0026_image_registry_kind
Revises: 0025_image_capacity
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_image_registry_kind"
down_revision: str | None = "0025_image_capacity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_BACKEND = (
    "(kind = 'language_model' AND backend IN ('mlx_lm', 'mlx_vlm')) OR "
    "(kind = 'image_model' AND backend = 'mflux' AND source = 'studio') OR "
    "(kind IN ('image_lora', 'control_model', 'upscale_model', 'image_classifier') "
    "AND backend = 'auxiliary' AND source = 'studio')"
)


def upgrade() -> None:
    op.add_column(
        "models",
        sa.Column("kind", sa.String(32), nullable=False, server_default="language_model"),
    )
    op.create_check_constraint("ck_models_kind_backend", "models", _KIND_BACKEND)


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM models WHERE kind <> 'language_model'")
    )
    if count:
        raise RuntimeError("remove image assets before downgrading registry kind")
    op.drop_constraint("ck_models_kind_backend", "models", type_="check")
    op.drop_column("models", "kind")
