"""Users, API keys, limits, entitlements, and enriched audit.

Revision ID: 0009_identity
Revises: 0008_sharded_serving
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_identity"
down_revision: str | None = "0008_sharded_serving"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM("admin", "user", name="user_role").create(bind, checkfirst=True)
    postgresql.ENUM("user", "api_key", "service", "anonymous", name="audit_actor_type").create(
        bind, checkfirst=True
    )
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("role", _enum("user_role", "admin", "user"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_role", "users", ["role"])
    op.create_index("ix_users_active", "users", ["active"])
    op.create_table(
        "entitlements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column(
            "granted_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_entitlements_user_id", "entitlements", ["user_id"])
    op.create_index("ix_entitlements_user_name", "entitlements", ["user_id", "name"])
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("prefix", sa.String(12), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scopes", postgresql.ARRAY(sa.String(32)), nullable=False),
        sa.Column("requests_per_minute", sa.Integer(), nullable=False),
        sa.Column("monthly_budget_tokens", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("requests_per_minute > 0", name="ck_api_keys_positive_rate"),
        sa.CheckConstraint("monthly_budget_tokens > 0", name="ck_api_keys_positive_budget"),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_index("ix_api_keys_prefix", "api_keys", ["prefix"])
    op.create_table(
        "api_key_rate_windows",
        sa.Column(
            "api_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "api_key_usage_accumulators",
        sa.Column(
            "api_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "audit_log",
        sa.Column(
            "actor_type",
            _enum("audit_actor_type", "user", "api_key", "service", "anonymous"),
            nullable=False,
            server_default="service",
        ),
    )
    op.execute("UPDATE audit_log SET actor_type = 'service' WHERE actor_type IS NULL")
    op.add_column(
        "audit_log",
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "audit_log", sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    for name in ("before", "after", "context"):
        op.add_column(
            "audit_log",
            sa.Column(
                name, postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
            ),
        )
    op.create_index("ix_audit_log_actor_user_id", "audit_log", ["actor_user_id"])
    op.create_index("ix_audit_log_request_id", "audit_log", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_request_id", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_user_id", table_name="audit_log")
    for name in ("context", "after", "before", "request_id", "actor_user_id", "actor_type"):
        op.drop_column("audit_log", name)
    op.drop_table("api_key_usage_accumulators")
    op.drop_table("api_key_rate_windows")
    op.drop_table("api_keys")
    op.drop_table("entitlements")
    op.drop_table("users")
    bind = op.get_bind()
    postgresql.ENUM(name="audit_actor_type").drop(bind, checkfirst=True)
    postgresql.ENUM(name="user_role").drop(bind, checkfirst=True)
