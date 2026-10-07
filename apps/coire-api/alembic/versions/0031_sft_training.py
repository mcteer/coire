"""Immutable SFT inputs, fenced execution, mirrored artifacts and exact subjects.

Revision ID: 0031_sft_training
Revises: 0030_image_output_retention

The SQL below is a frozen PostgreSQL schema snapshot, independent of future ORM changes.
No tensor bytes are stored in these tables. Downgrade refuses outstanding work/artifacts.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031_sft_training"
down_revision: str | None = "0030_image_output_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_ORDER = (
    "training_dataset_revisions",
    "training_measurements",
    "training_storage_reservations",
    "training_dataset_analyses",
    "training_jobs",
    "training_profiles",
    "training_attempts",
    "training_dataset_references",
    "training_checkpoints",
    "training_commands",
    "training_dataset_grants",
    "training_events",
    "training_metrics",
    "training_participants",
    "training_transfer_grants",
    "training_adapters",
    "training_artifact_copies",
    "training_eviction_intents",
)

UPGRADE_SQL = (
    """CREATE TABLE training_dataset_revisions (
        id UUID PRIMARY KEY, owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        name VARCHAR(120) NOT NULL, format VARCHAR(32) NOT NULL,
        state VARCHAR(32) NOT NULL DEFAULT 'uploading', source_sha256 VARCHAR(64),
        source_bytes BIGINT NOT NULL DEFAULT 0, storage_key VARCHAR(128) NOT NULL UNIQUE,
        provenance JSONB NOT NULL, row_count INTEGER NOT NULL DEFAULT 0,
        split_seed BIGINT NOT NULL, validation_fraction FLOAT NOT NULL,
        split_manifest JSONB, split_sha256 VARCHAR(64), version INTEGER NOT NULL DEFAULT 1,
        safe_failure_code VARCHAR(64), diagnostics JSONB NOT NULL DEFAULT '[]'::jsonb,
        invalid_count INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        purged_at TIMESTAMPTZ,
        CONSTRAINT ck_training_dataset_bounds CHECK (version >= 1 AND source_bytes >= 0 AND row_count >= 0),
        CONSTRAINT ck_training_dataset_state CHECK (state IN ('uploading','validating','analyzing','ready','analysis_failed','failed','retired','purged'))
    )""",
    """CREATE TABLE training_measurements (
        id UUID PRIMARY KEY, owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        request JSONB NOT NULL, state VARCHAR(16) NOT NULL DEFAULT 'queued', report JSONB,
        report_sha256 VARCHAR(64), created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE training_storage_reservations (
        id UUID PRIMARY KEY, owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        node_id UUID REFERENCES nodes(id) ON DELETE RESTRICT, subject_id VARCHAR(128) NOT NULL,
        bytes BIGINT NOT NULL, state VARCHAR(16) NOT NULL DEFAULT 'held',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), released_at TIMESTAMPTZ,
        CONSTRAINT ck_training_storage_positive CHECK (bytes > 0)
    )""",
    """CREATE TABLE training_dataset_analyses (
        id UUID PRIMARY KEY, dataset_id UUID NOT NULL REFERENCES training_dataset_revisions(id) ON DELETE RESTRICT,
        model_id UUID NOT NULL REFERENCES models(id) ON DELETE RESTRICT,
        variant_id UUID NOT NULL REFERENCES model_variants(id) ON DELETE RESTRICT,
        command_id UUID NOT NULL, identity_sha256 VARCHAR(64) NOT NULL,
        tokenizer_sha256 VARCHAR(64), template_sha256 VARCHAR(64), runtime_sha256 VARCHAR(64),
        state VARCHAR(16) NOT NULL DEFAULT 'queued', result JSONB, safe_failure_code VARCHAR(64),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_training_analysis_identity UNIQUE(dataset_id,identity_sha256,command_id)
    )""",
    """CREATE TABLE training_jobs (
        id VARCHAR(26) PRIMARY KEY, owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        originating_key_id UUID REFERENCES api_keys(id) ON DELETE SET NULL, originating_key_version INTEGER,
        model_id UUID NOT NULL REFERENCES models(id) ON DELETE RESTRICT, base_variant_id UUID NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL, output_slug VARCHAR(63) NOT NULL, source_yaml TEXT NOT NULL,
        source_sha256 VARCHAR(64) NOT NULL, intent_sha256 VARCHAR(64) NOT NULL, submitted_spec JSONB NOT NULL,
        resolved_spec JSONB, resolved_sha256 VARCHAR(64), authorization_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
        state VARCHAR(16) NOT NULL DEFAULT 'queued', version INTEGER NOT NULL DEFAULT 1,
        fence BIGINT NOT NULL DEFAULT 0, completed_update INTEGER NOT NULL DEFAULT 0,
        recovery_attempts INTEGER NOT NULL DEFAULT 0, cumulative_execution_seconds FLOAT NOT NULL DEFAULT 0,
        pause_origin VARCHAR(16), safe_reason VARCHAR(64), queue_deadline_at TIMESTAMPTZ NOT NULL,
        execution_deadline_at TIMESTAMPTZ NOT NULL, latest_checkpoint_id UUID, adapter_id UUID,
        next_event_sequence BIGINT NOT NULL DEFAULT 1, reproducible BOOLEAN NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ, deleted_at TIMESTAMPTZ,
        CONSTRAINT uq_training_job_owner_key UNIQUE(owner_user_id,idempotency_key),
        CONSTRAINT uq_training_output_namespace UNIQUE(model_id,output_slug),
        CONSTRAINT fk_training_job_exact_base FOREIGN KEY(base_variant_id,model_id)
            REFERENCES model_variants(id,model_id) ON DELETE RESTRICT,
        CONSTRAINT ck_training_job_bounds CHECK(version >= 1 AND fence >= 0 AND completed_update >= 0),
        CONSTRAINT ck_training_job_state CHECK(state IN ('queued','preflighting','reserving','running','pausing','paused','recovering','finalizing','cancelling','succeeded','failed','cancelled'))
    )""",
    """CREATE TABLE training_profiles (
        id UUID PRIMARY KEY, measurement_id UUID NOT NULL REFERENCES training_measurements(id) ON DELETE RESTRICT,
        report_sha256 VARCHAR(64) NOT NULL UNIQUE, identity_sha256 VARCHAR(64) NOT NULL, profile JSONB NOT NULL,
        valid_until TIMESTAMPTZ NOT NULL, invalidated_reason VARCHAR(64), created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE training_attempts (
        id VARCHAR(26) PRIMARY KEY, job_id VARCHAR(26) NOT NULL REFERENCES training_jobs(id) ON DELETE RESTRICT,
        generation INTEGER NOT NULL, fence BIGINT NOT NULL, world_size INTEGER NOT NULL,
        runtime_sha256 VARCHAR(64) NOT NULL, resume_checkpoint_id UUID, state VARCHAR(16) NOT NULL,
        lease_expires_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), stopped_at TIMESTAMPTZ,
        CONSTRAINT uq_training_attempt_generation UNIQUE(job_id,generation),
        CONSTRAINT uq_training_attempt_fence UNIQUE(job_id,fence),
        CONSTRAINT ck_training_attempt_bounds CHECK(generation >= 1 AND fence >= 1 AND world_size IN (1,2))
    )""",
    """CREATE TABLE training_dataset_references (
        job_id VARCHAR(26) NOT NULL REFERENCES training_jobs(id) ON DELETE RESTRICT,
        dataset_id UUID NOT NULL REFERENCES training_dataset_revisions(id) ON DELETE RESTRICT,
        analysis_id UUID NOT NULL REFERENCES training_dataset_analyses(id) ON DELETE RESTRICT,
        source_sha256 VARCHAR(64) NOT NULL, split_sha256 VARCHAR(64) NOT NULL, PRIMARY KEY(job_id,dataset_id)
    )""",
    """CREATE TABLE training_checkpoints (
        id UUID PRIMARY KEY, job_id VARCHAR(26) NOT NULL REFERENCES training_jobs(id) ON DELETE RESTRICT,
        attempt_id VARCHAR(26) NOT NULL REFERENCES training_attempts(id) ON DELETE RESTRICT,
        fence BIGINT NOT NULL, completed_update INTEGER NOT NULL, manifest_sha256 VARCHAR(64) NOT NULL UNIQUE,
        manifest JSONB NOT NULL, total_bytes BIGINT NOT NULL, state VARCHAR(16) NOT NULL DEFAULT 'staging',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), committed_at TIMESTAMPTZ, purged_at TIMESTAMPTZ,
        CONSTRAINT uq_training_checkpoint_update UNIQUE(job_id,attempt_id,completed_update),
        CONSTRAINT ck_training_checkpoint_bounds CHECK(fence >= 1 AND completed_update >= 0 AND total_bytes > 0)
    )""",
    """CREATE TABLE training_commands (
        id UUID PRIMARY KEY, actor_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        idempotency_key VARCHAR(128) NOT NULL, operation VARCHAR(64) NOT NULL, subject_id VARCHAR(128) NOT NULL,
        request_sha256 VARCHAR(64) NOT NULL, job_id VARCHAR(26) REFERENCES training_jobs(id) ON DELETE RESTRICT,
        attempt_id VARCHAR(26) REFERENCES training_attempts(id) ON DELETE RESTRICT, payload JSONB NOT NULL,
        receipt JSONB, state VARCHAR(16) NOT NULL DEFAULT 'pending', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_training_command_actor_key UNIQUE(actor_user_id,idempotency_key,operation)
    )""",
    """CREATE TABLE training_dataset_grants (
        id UUID PRIMARY KEY, secret_hash VARCHAR(64) NOT NULL UNIQUE, node_id UUID NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT,
        dataset_id UUID NOT NULL REFERENCES training_dataset_revisions(id) ON DELETE RESTRICT,
        analysis_id UUID REFERENCES training_dataset_analyses(id) ON DELETE RESTRICT,
        attempt_id VARCHAR(26) REFERENCES training_attempts(id) ON DELETE RESTRICT,
        source_sha256 VARCHAR(64) NOT NULL, max_bytes BIGINT NOT NULL, expires_at TIMESTAMPTZ NOT NULL, revoked_at TIMESTAMPTZ,
        CONSTRAINT ck_training_dataset_grant_subject CHECK((analysis_id IS NULL) <> (attempt_id IS NULL))
    )""",
    """CREATE TABLE training_events (
        job_id VARCHAR(26) NOT NULL REFERENCES training_jobs(id) ON DELETE RESTRICT, sequence BIGINT NOT NULL,
        attempt_id VARCHAR(26) REFERENCES training_attempts(id) ON DELETE RESTRICT, fence BIGINT,
        state_version INTEGER NOT NULL, payload JSONB NOT NULL, occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(job_id,sequence), CONSTRAINT ck_training_event_sequence CHECK(sequence >= 1 AND state_version >= 1)
    )""",
    """CREATE TABLE training_metrics (
        id UUID PRIMARY KEY, job_id VARCHAR(26) NOT NULL REFERENCES training_jobs(id) ON DELETE RESTRICT,
        attempt_id VARCHAR(26) NOT NULL REFERENCES training_attempts(id) ON DELETE RESTRICT,
        completed_update INTEGER NOT NULL, kind VARCHAR(16) NOT NULL, loss FLOAT NOT NULL, metric JSONB NOT NULL,
        rolled_back BOOLEAN NOT NULL DEFAULT false, recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_training_metric_update UNIQUE(job_id,attempt_id,completed_update,kind),
        CONSTRAINT ck_training_metric_kind CHECK(completed_update >= 0 AND kind IN ('train','validation'))
    )""",
    """CREATE TABLE training_participants (
        id UUID PRIMARY KEY, attempt_id VARCHAR(26) NOT NULL REFERENCES training_attempts(id) ON DELETE RESTRICT,
        node_id UUID NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT, rank INTEGER NOT NULL,
        reservation_id UUID NOT NULL REFERENCES memory_reservations(id) ON DELETE RESTRICT, disk_reservation_id UUID,
        command_id UUID NOT NULL, request_sha256 VARCHAR(64) NOT NULL, spawn_nonce UUID NOT NULL,
        pid INTEGER, process_create_time FLOAT, footprint_bytes BIGINT, stop_proof JSONB, stopped_at TIMESTAMPTZ,
        CONSTRAINT uq_training_participant_rank UNIQUE(attempt_id,rank),
        CONSTRAINT uq_training_participant_node UNIQUE(attempt_id,node_id),
        CONSTRAINT ck_training_participant_rank CHECK(rank IN (0,1)),
        CONSTRAINT ck_training_participant_stop_proof CHECK(stopped_at IS NULL OR stop_proof IS NOT NULL)
    )""",
    """CREATE TABLE training_transfer_grants (
        id UUID PRIMARY KEY, secret_hash VARCHAR(64) NOT NULL UNIQUE,
        source_node_id UUID NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT,
        destination_node_id UUID NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT,
        attempt_id VARCHAR(26) NOT NULL REFERENCES training_attempts(id) ON DELETE RESTRICT,
        fence BIGINT NOT NULL, artifact_id UUID NOT NULL, manifest_sha256 VARCHAR(64) NOT NULL,
        file_ids JSONB NOT NULL, max_bytes BIGINT NOT NULL, expires_at TIMESTAMPTZ NOT NULL, revoked_at TIMESTAMPTZ,
        CONSTRAINT ck_training_transfer_scope CHECK(source_node_id <> destination_node_id AND fence >= 1)
    )""",
    """CREATE TABLE training_adapters (
        id UUID PRIMARY KEY, model_id UUID NOT NULL REFERENCES models(id) ON DELETE RESTRICT,
        base_variant_id UUID NOT NULL, source_job_id VARCHAR(26) NOT NULL REFERENCES training_jobs(id) ON DELETE RESTRICT,
        source_checkpoint_id UUID REFERENCES training_checkpoints(id) ON DELETE RESTRICT,
        slug VARCHAR(63) NOT NULL, selector VARCHAR(100) NOT NULL UNIQUE, base_manifest_sha256 VARCHAR(64) NOT NULL,
        manifest_sha256 VARCHAR(64), resolved_spec_sha256 VARCHAR(64) NOT NULL, parameterization VARCHAR(16) NOT NULL,
        objective VARCHAR(16) NOT NULL DEFAULT 'sft', state VARCHAR(16) NOT NULL DEFAULT 'validating',
        visibility VARCHAR(16) NOT NULL DEFAULT 'admin_only', verified BOOLEAN NOT NULL DEFAULT false,
        evaluation_id UUID, version INTEGER NOT NULL DEFAULT 1, metadata_record JSONB NOT NULL DEFAULT '{}'::jsonb,
        required_entitlements JSONB NOT NULL DEFAULT '[]'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_training_adapter_model_slug UNIQUE(model_id,slug),
        CONSTRAINT fk_training_adapter_exact_base FOREIGN KEY(base_variant_id,model_id)
            REFERENCES model_variants(id,model_id) ON DELETE RESTRICT,
        CONSTRAINT ck_training_adapter_verification CHECK(version >= 1 AND (NOT verified OR evaluation_id IS NOT NULL)),
        CONSTRAINT ck_training_adapter_publication CHECK(visibility <> 'published' OR (state = 'ready' AND manifest_sha256 IS NOT NULL))
    )""",
    """CREATE TABLE training_artifact_copies (
        id UUID PRIMARY KEY, artifact_id UUID NOT NULL,
        checkpoint_id UUID REFERENCES training_checkpoints(id) ON DELETE RESTRICT,
        adapter_id UUID REFERENCES training_adapters(id) ON DELETE RESTRICT,
        node_id UUID NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT, manifest_sha256 VARCHAR(64) NOT NULL,
        storage_key VARCHAR(128) NOT NULL, total_bytes BIGINT NOT NULL, state VARCHAR(16) NOT NULL DEFAULT 'pending',
        verified_at TIMESTAMPTZ, cleanup_receipt JSONB,
        CONSTRAINT uq_training_artifact_copy_node UNIQUE(artifact_id,node_id),
        CONSTRAINT ck_training_artifact_subject CHECK((checkpoint_id IS NOT NULL AND adapter_id IS NULL) OR (adapter_id IS NOT NULL AND checkpoint_id IS NULL)),
        CONSTRAINT ck_training_artifact_copy_bytes CHECK(total_bytes > 0)
    )""",
    """CREATE TABLE training_eviction_intents (
        id UUID PRIMARY KEY, attempt_id VARCHAR(26) NOT NULL REFERENCES training_attempts(id) ON DELETE RESTRICT,
        instance_id UUID NOT NULL REFERENCES model_instances(id) ON DELETE RESTRICT,
        reservation_id UUID NOT NULL REFERENCES memory_reservations(id) ON DELETE RESTRICT,
        target JSONB NOT NULL, prior_policy VARCHAR(64) NOT NULL, prior_version VARCHAR(128) NOT NULL,
        restoration_state VARCHAR(16) NOT NULL DEFAULT 'pending'
    )""",
    "CREATE INDEX ix_training_dataset_owner_created ON training_dataset_revisions(owner_user_id,created_at,id)",
    "CREATE INDEX ix_training_jobs_owner_created ON training_jobs(owner_user_id,created_at,id)",
    "CREATE INDEX ix_training_jobs_state_queue ON training_jobs(state,queue_deadline_at)",
    "CREATE INDEX ix_training_profiles_identity_sha256 ON training_profiles(identity_sha256)",
    "CREATE UNIQUE INDEX uq_training_attempt_active ON training_attempts(job_id) WHERE state IN ('preparing','running','stopping','unknown')",
)

# Existing rows default to a base-only target; neither adapters nor verification are inferred.
UUID_COLUMNS = (
    ("harness_evaluations", "adapter_id", "training_adapters", "RESTRICT"),
    ("usage_records", "variant_id", "model_variants", "SET NULL"),
    ("usage_records", "adapter_id", "training_adapters", "RESTRICT"),
    ("usage_records", "instance_id", "model_instances", "SET NULL"),
    ("agent_runs", "primary_adapter_id", "training_adapters", "RESTRICT"),
    ("model_instances", "adapter_id", "training_adapters", "RESTRICT"),
    ("engine_processes", "variant_id", "model_variants", "RESTRICT"),
    ("engine_processes", "adapter_id", "training_adapters", "RESTRICT"),
)
JSON_COLUMNS = (
    ("mcp_calls", "target"),
    ("chat_conversations", "selected_target"),
    ("chat_messages", "target"),
    ("chat_turns", "target"),
)
DEFERRED_FKS = (
    (
        "fk_training_latest_checkpoint",
        "training_jobs",
        "latest_checkpoint_id",
        "training_checkpoints",
    ),
    ("fk_training_final_adapter", "training_jobs", "adapter_id", "training_adapters"),
    (
        "fk_training_resume_checkpoint",
        "training_attempts",
        "resume_checkpoint_id",
        "training_checkpoints",
    ),
    ("fk_training_adapter_evaluation", "training_adapters", "evaluation_id", "harness_evaluations"),
)


def upgrade() -> None:
    op.add_column(
        "node_memory_ledgers", sa.Column("swap_used_bytes", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "usage_records", sa.Column("first_token_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("usage_records", sa.Column("first_token_duration_ms", sa.Float(), nullable=True))
    op.create_unique_constraint("uq_variant_identity_model", "model_variants", ["id", "model_id"])
    for statement in UPGRADE_SQL:
        op.execute(statement)
    for name, source, column, destination in DEFERRED_FKS:
        op.create_foreign_key(name, source, destination, [column], ["id"], ondelete="RESTRICT")
    for table, column, destination, deletion in UUID_COLUMNS:
        op.add_column(table, sa.Column(column, postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_{column}_training", table, destination, [column], ["id"], ondelete=deletion
        )
    op.add_column(
        "harness_evaluations", sa.Column("subject_manifest_sha256", sa.String(64), nullable=True)
    )
    for table, column in JSON_COLUMNS:
        op.add_column(table, sa.Column(column, postgresql.JSONB(none_as_null=True), nullable=True))
    op.create_index(
        "ix_usage_records_instance_first_token", "usage_records", ["instance_id", "first_token_at"]
    )
    op.execute("""CREATE FUNCTION protect_training_job_intent() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
        IF ROW(NEW.owner_user_id,NEW.model_id,NEW.base_variant_id,NEW.idempotency_key,NEW.output_slug,NEW.source_yaml,NEW.source_sha256,NEW.intent_sha256,NEW.submitted_spec)
           IS DISTINCT FROM ROW(OLD.owner_user_id,OLD.model_id,OLD.base_variant_id,OLD.idempotency_key,OLD.output_slug,OLD.source_yaml,OLD.source_sha256,OLD.intent_sha256,OLD.submitted_spec)
           OR (OLD.resolved_spec IS NOT NULL AND ROW(NEW.resolved_spec,NEW.resolved_sha256) IS DISTINCT FROM ROW(OLD.resolved_spec,OLD.resolved_sha256)) THEN
            RAISE EXCEPTION 'training intent and resolved inputs are immutable';
        END IF;
        RETURN NEW; END; $$""")
    op.execute(
        "CREATE TRIGGER training_job_immutable BEFORE UPDATE ON training_jobs FOR EACH ROW EXECUTE FUNCTION protect_training_job_intent()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM training_jobs WHERE state NOT IN ('succeeded','failed','cancelled'))"
        )
    ).scalar():
        raise RuntimeError("stop and reconcile live training jobs before downgrade")
    for table in (
        "training_adapters",
        "training_checkpoints",
        "training_artifact_copies",
        "training_dataset_revisions",
    ):
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("retained training artifacts and dataset lineage prevent downgrade")
    for table, column, _destination, _deletion in reversed(UUID_COLUMNS):
        if table == "usage_records" and column == "instance_id":
            continue
        if bind.execute(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {column} IS NOT NULL)")
        ).scalar():
            raise RuntimeError("exact inference target references prevent downgrade")
    for table, column in JSON_COLUMNS:
        if bind.execute(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {column} IS NOT NULL)")
        ).scalar():
            raise RuntimeError("exact chat/MCP target references prevent downgrade")
    for table, column in reversed(JSON_COLUMNS):
        op.drop_column(table, column)
    op.drop_column("harness_evaluations", "subject_manifest_sha256")
    op.drop_index("ix_usage_records_instance_first_token", table_name="usage_records")
    op.drop_column("usage_records", "first_token_at")
    op.drop_column("usage_records", "first_token_duration_ms")
    op.drop_column("node_memory_ledgers", "swap_used_bytes")
    for table, column, _destination, _deletion in reversed(UUID_COLUMNS):
        op.drop_constraint(f"fk_{table}_{column}_training", table, type_="foreignkey")
        op.drop_column(table, column)
    for name, source, _column, _destination in reversed(DEFERRED_FKS):
        op.drop_constraint(name, source, type_="foreignkey")
    op.execute("DROP TRIGGER training_job_immutable ON training_jobs")
    op.execute("DROP FUNCTION protect_training_job_intent()")
    for table in reversed(TABLE_ORDER):
        op.drop_table(table)
    op.drop_constraint("uq_variant_identity_model", "model_variants", type_="unique")
