"""Durable Studio evaluation history. Frozen SQL; no runtime ORM imports."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_evaluation_verbs"
down_revision: str | None = "0031_sft_training"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_ORDER = (
    "evaluation_measurements",
    "evaluation_mutations",
    "evaluation_suites",
    "evaluation_coexistence_profiles",
    "evaluation_groups",
    "evaluation_group_events",
    "evaluation_runs",
    "training_evaluation_triggers",
    "evaluation_attempts",
    "evaluation_checkpoint_pins",
    "evaluation_events",
    "evaluation_results",
    "evaluation_evidence",
)
UPGRADE_SQL = (
    """CREATE TABLE evaluation_measurements (
	id UUID NOT NULL, 
	owner_user_id UUID NOT NULL, 
	request JSONB NOT NULL, 
	authorization_snapshot JSONB DEFAULT '{}'::jsonb NOT NULL, 
	execution JSONB DEFAULT '{}'::jsonb NOT NULL, 
	version INTEGER DEFAULT '1' NOT NULL, 
	deadline_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	state VARCHAR(16) DEFAULT 'queued' NOT NULL, 
	report JSONB, 
	report_sha256 VARCHAR(64), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE evaluation_mutations (
	id UUID NOT NULL, 
	owner_user_id UUID NOT NULL, 
	operation VARCHAR(128) NOT NULL, 
	key_sha256 VARCHAR(64) NOT NULL, 
	request_sha256 VARCHAR(64) NOT NULL, 
	response JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_evaluation_mutation_key UNIQUE (owner_user_id, operation, key_sha256), 
	FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE evaluation_suites (
	id UUID NOT NULL, 
	suite_id VARCHAR(64) NOT NULL, 
	version INTEGER NOT NULL, 
	registry_version INTEGER DEFAULT '1' NOT NULL, 
	definition JSONB NOT NULL, 
	content_sha256 VARCHAR(64) NOT NULL, 
	owner_user_id UUID, 
	attribution VARCHAR(64) DEFAULT 'admin' NOT NULL, 
	retired BOOLEAN DEFAULT false NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_evaluation_suite_version UNIQUE (suite_id, version), 
	CONSTRAINT ck_evaluation_suite_version CHECK (version >= 1), 
	FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE evaluation_coexistence_profiles (
	id UUID NOT NULL, 
	measurement_id UUID NOT NULL, 
	fingerprint_sha256 VARCHAR(64) NOT NULL, 
	report_sha256 VARCHAR(64) NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	invalidated_at TIMESTAMP WITH TIME ZONE, 
	invalidated_reason VARCHAR(64), 
	PRIMARY KEY (id), 
	UNIQUE (measurement_id), 
	FOREIGN KEY(measurement_id) REFERENCES evaluation_measurements (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE evaluation_groups (
	id VARCHAR(26) NOT NULL, 
	owner_user_id UUID NOT NULL, 
	origin VARCHAR(32) NOT NULL, 
	job_id VARCHAR(26), 
	checkpoint_id UUID, 
	completed_update INTEGER, 
	subjects JSONB NOT NULL, 
	next_event_sequence BIGINT DEFAULT '1' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_evaluation_group_sequence CHECK (next_event_sequence >= 1), 
	CONSTRAINT ck_evaluation_group_origin CHECK (origin IN ('manual','training_final','training_checkpoint','measurement')), 
	FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT, 
	FOREIGN KEY(job_id) REFERENCES training_jobs (id) ON DELETE RESTRICT, 
	FOREIGN KEY(checkpoint_id) REFERENCES training_checkpoints (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE evaluation_group_events (
	id UUID NOT NULL, 
	group_id VARCHAR(26) NOT NULL, 
	sequence BIGINT NOT NULL, 
	payload JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_evaluation_group_event_sequence UNIQUE (group_id, sequence), 
	CONSTRAINT ck_evaluation_group_event_sequence CHECK (sequence >= 1), 
	FOREIGN KEY(group_id) REFERENCES evaluation_groups (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE evaluation_runs (
	id VARCHAR(26) NOT NULL, 
	group_id VARCHAR(26) NOT NULL, 
	owner_user_id UUID NOT NULL, 
	suite_row_id UUID NOT NULL, 
	suite_snapshot JSONB NOT NULL, 
	subjects JSONB NOT NULL, 
	source_run_id VARCHAR(26), 
	measurement_id UUID, 
	authorization_snapshot JSONB NOT NULL, 
	next_event_sequence BIGINT DEFAULT '1' NOT NULL, 
	request_sha256 VARCHAR(64) NOT NULL, 
	idempotency_key_sha256 VARCHAR(64) NOT NULL, 
	state VARCHAR(16) DEFAULT 'queued' NOT NULL, 
	phase VARCHAR(32), 
	version INTEGER DEFAULT '1' NOT NULL, 
	fence BIGINT DEFAULT '1' NOT NULL, 
	cleanup_state VARCHAR(16) DEFAULT 'complete' NOT NULL, 
	safe_failure_code VARCHAR(64), 
	evidence_reserved_bytes BIGINT DEFAULT '0' NOT NULL, 
	data_snapshot JSONB, 
	queue_deadline_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	execution_deadline_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	started_deadline_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_evaluation_run_idempotency UNIQUE (owner_user_id, idempotency_key_sha256), 
	CONSTRAINT ck_evaluation_run_bounds CHECK (version >= 1 AND fence >= 1 AND evidence_reserved_bytes >= 0 AND evidence_reserved_bytes <= 8388608), 
	CONSTRAINT ck_evaluation_run_state CHECK (state IN ('queued','preparing','reserving','running','collecting','cancelling','succeeded','failed','timed_out','cancelled')), 
	FOREIGN KEY(group_id) REFERENCES evaluation_groups (id) ON DELETE RESTRICT, 
	FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT, 
	FOREIGN KEY(suite_row_id) REFERENCES evaluation_suites (id) ON DELETE RESTRICT, 
	FOREIGN KEY(source_run_id) REFERENCES evaluation_runs (id) ON DELETE RESTRICT, 
	FOREIGN KEY(measurement_id) REFERENCES evaluation_measurements (id) ON DELETE RESTRICT
)""",
    """CREATE INDEX ix_evaluation_runs_dispatch ON evaluation_runs (state, created_at)""",
    """CREATE TABLE training_evaluation_triggers (
	id UUID NOT NULL, 
	job_id VARCHAR(26) NOT NULL, 
	checkpoint_id UUID, 
	boundary_kind VARCHAR(16) NOT NULL, 
	completed_update INTEGER NOT NULL, 
	schedule_sha256 VARCHAR(64) NOT NULL, 
	schedules JSONB NOT NULL, 
	group_id VARCHAR(26), 
	phase VARCHAR(32) DEFAULT 'pending_pause' NOT NULL, 
	fence BIGINT NOT NULL, 
	pause_version INTEGER, 
	resume_disposition VARCHAR(32), 
	deadline_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_training_evaluation_boundary UNIQUE (job_id, boundary_kind, completed_update, schedule_sha256), 
	CONSTRAINT ck_training_evaluation_boundary CHECK (completed_update >= 1 AND fence >= 1), 
	FOREIGN KEY(job_id) REFERENCES training_jobs (id) ON DELETE RESTRICT, 
	FOREIGN KEY(checkpoint_id) REFERENCES training_checkpoints (id) ON DELETE RESTRICT, 
	UNIQUE (group_id), 
	FOREIGN KEY(group_id) REFERENCES evaluation_groups (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE evaluation_attempts (
	id UUID NOT NULL, 
	run_id VARCHAR(26) NOT NULL, 
	phase VARCHAR(32) NOT NULL, 
	ordinal INTEGER NOT NULL, 
	fence BIGINT NOT NULL, 
	agent_run_id UUID, 
	node_id UUID, 
	instance_id UUID, 
	target_sha256 VARCHAR(64) NOT NULL, 
	profile_id UUID, 
	owns_instance BOOLEAN DEFAULT false NOT NULL, 
	sandbox_reservation_id UUID, 
	lease_id UUID, 
	resident_lease_ids JSONB DEFAULT '[]'::jsonb NOT NULL, 
	workload JSONB NOT NULL, 
	workspace_ref VARCHAR(64), 
	output_ref VARCHAR(64), 
	collected_sha256 VARCHAR(64), 
	state VARCHAR(16) DEFAULT 'pending' NOT NULL, 
	deadline_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_evaluation_attempt_phase UNIQUE (run_id, phase, ordinal), 
	CONSTRAINT ck_evaluation_attempt_fence CHECK (ordinal >= 1 AND fence >= 1), 
	FOREIGN KEY(run_id) REFERENCES evaluation_runs (id) ON DELETE RESTRICT, 
	UNIQUE (agent_run_id), 
	FOREIGN KEY(agent_run_id) REFERENCES agent_runs (id) ON DELETE RESTRICT, 
	FOREIGN KEY(node_id) REFERENCES nodes (id) ON DELETE RESTRICT, 
	FOREIGN KEY(instance_id) REFERENCES model_instances (id) ON DELETE RESTRICT, 
	FOREIGN KEY(profile_id) REFERENCES evaluation_coexistence_profiles (id) ON DELETE RESTRICT, 
	FOREIGN KEY(sandbox_reservation_id) REFERENCES memory_reservations (id) ON DELETE RESTRICT, 
	FOREIGN KEY(lease_id) REFERENCES request_leases (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE evaluation_checkpoint_pins (
	id UUID NOT NULL, 
	trigger_id UUID NOT NULL, 
	checkpoint_id UUID NOT NULL, 
	released_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_evaluation_checkpoint_pin UNIQUE (trigger_id, checkpoint_id), 
	FOREIGN KEY(trigger_id) REFERENCES training_evaluation_triggers (id) ON DELETE RESTRICT, 
	FOREIGN KEY(checkpoint_id) REFERENCES training_checkpoints (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE evaluation_events (
	id UUID NOT NULL, 
	run_id VARCHAR(26) NOT NULL, 
	sequence BIGINT NOT NULL, 
	payload JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_evaluation_event_sequence UNIQUE (run_id, sequence), 
	FOREIGN KEY(run_id) REFERENCES evaluation_runs (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE evaluation_results (
	id VARCHAR(26) NOT NULL, 
	run_id VARCHAR(26) NOT NULL, 
	fence BIGINT NOT NULL, 
	result JSONB NOT NULL, 
	result_sha256 VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (run_id), 
	FOREIGN KEY(run_id) REFERENCES evaluation_runs (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE evaluation_evidence (
	id UUID NOT NULL, 
	run_id VARCHAR(26) NOT NULL, 
	attempt_id UUID NOT NULL, 
	storage_key VARCHAR(128) NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	bytes BIGINT NOT NULL, 
	availability VARCHAR(16) DEFAULT 'present' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	pin_until TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_evaluation_evidence_bytes CHECK (bytes >= 0 AND bytes <= 8388608), 
	FOREIGN KEY(run_id) REFERENCES evaluation_runs (id) ON DELETE RESTRICT, 
	FOREIGN KEY(attempt_id) REFERENCES evaluation_attempts (id) ON DELETE RESTRICT, 
	UNIQUE (storage_key)
)""",
)

ADDITIONS = (
    "ALTER TABLE agent_runs ADD COLUMN purpose VARCHAR(16) NOT NULL DEFAULT 'harness'",
    "ALTER TABLE agent_runs ADD COLUMN evaluation_attempt_id UUID",
    "ALTER TABLE training_jobs ADD COLUMN evaluation_pause_trigger_id UUID",
    "ALTER TABLE training_adapters ADD COLUMN purpose VARCHAR(16) NOT NULL DEFAULT 'serving'",
    "ALTER TABLE training_adapters ADD COLUMN evaluation_trigger_id UUID",
    "ALTER TABLE training_adapters ADD CONSTRAINT fk_training_adapter_evaluation_trigger FOREIGN KEY (evaluation_trigger_id) REFERENCES training_evaluation_triggers(id) ON DELETE RESTRICT",
    "ALTER TABLE training_adapters ADD CONSTRAINT ck_training_adapter_evaluation_purpose CHECK ((purpose = 'serving' AND evaluation_trigger_id IS NULL) OR (purpose = 'evaluation' AND evaluation_trigger_id IS NOT NULL AND visibility = 'admin_only' AND NOT verified))",
)


BUILTIN_HARNESS = '{"suite_id":"harness-capability","version":1,"registry_version":1,"template":{"template_id":"harness-capability","template_version":1,"kind":"harness","mode":"capability","content_sha256":"ddea8bd3f6ca4f4744dee6ebcd0033746e099cebfff773e6b2cff0799664b284","cases_sha256":"c48e4d6f3ae10c0bdd950fef9397176913a688ead39f002dbeca421c936558d7","scorer_version":"coire-evaluation-v1","case_count":4,"license":"Project-authored fixtures; no third-party benchmark data"},"generation":{"temperature":0.0,"top_p":1.0,"seed":0,"max_tokens":512,"stop":[]},"timeout_seconds":900,"judge":null,"judge_generation":{"temperature":0.0,"top_p":1.0,"seed":0,"max_tokens":512,"stop":[]},"content_sha256":"2c6560bc2cee0743891684783031393f9a3d1580c9142766f82e7d497a921691","retired":false,"registered_at":"2026-10-07T00:00:00Z","registered_by":null}'


def upgrade() -> None:
    for statement in UPGRADE_SQL + ADDITIONS:
        op.execute(statement)
    op.get_bind().execute(
        sa.text(
            "INSERT INTO evaluation_suites (id,suite_id,version,definition,content_sha256,attribution,created_at) VALUES ('00000000-0000-4000-8000-000000000017','harness-capability',1,CAST(:definition AS JSONB),:digest,'system:migration-0032','2026-10-07T00:00:00Z')"
        ),
        {
            "definition": BUILTIN_HARNESS,
            "digest": "2c6560bc2cee0743891684783031393f9a3d1580c9142766f82e7d497a921691",
        },
    )
    op.execute(
        "INSERT INTO audit_log (id,actor,actor_type,action,target_type,target_id,outcome,detail,before,after,context) SELECT '00000000-0000-4000-8000-000000000032','system:migration-0032','service','evaluation.suite.seed','evaluation_suite','harness-capability:1','ok','{}'::jsonb,'{}'::jsonb,'{}'::jsonb,'{}'::jsonb WHERE NOT EXISTS (SELECT 1 FROM audit_log WHERE id='00000000-0000-4000-8000-000000000032')"
    )
    op.execute("""CREATE FUNCTION protect_evaluation_result() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'evaluation result is immutable'; END; $$""")
    op.execute(
        "CREATE TRIGGER evaluation_result_immutable BEFORE UPDATE OR DELETE ON evaluation_results FOR EACH ROW EXECUTE FUNCTION protect_evaluation_result()"
    )
    op.execute("""CREATE FUNCTION fence_evaluation_result() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE current_fence BIGINT; current_state VARCHAR(16);
        BEGIN
        SELECT fence,state INTO current_fence,current_state FROM evaluation_runs WHERE id=NEW.run_id FOR UPDATE;
        IF current_fence IS DISTINCT FROM NEW.fence OR current_state NOT IN ('succeeded','failed','timed_out','cancelled') OR NEW.result->>'outcome' IS DISTINCT FROM current_state THEN
            RAISE EXCEPTION 'evaluation terminal commit has stale fence or outcome';
        END IF;
        RETURN NEW; END; $$""")
    op.execute(
        "CREATE TRIGGER evaluation_result_fence BEFORE INSERT ON evaluation_results FOR EACH ROW EXECUTE FUNCTION fence_evaluation_result()"
    )
    op.execute("""CREATE FUNCTION protect_evaluation_group_intent() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
        IF (to_jsonb(NEW) - 'next_event_sequence') IS DISTINCT FROM (to_jsonb(OLD) - 'next_event_sequence') THEN
            RAISE EXCEPTION 'evaluation group intent is immutable';
        END IF;
        RETURN NEW; END; $$""")
    op.execute(
        "CREATE TRIGGER evaluation_group_immutable BEFORE UPDATE ON evaluation_groups FOR EACH ROW EXECUTE FUNCTION protect_evaluation_group_intent()"
    )
    op.execute("""CREATE FUNCTION protect_training_evaluation_trigger() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
        IF ROW(NEW.job_id,NEW.checkpoint_id,NEW.boundary_kind,NEW.completed_update,
               NEW.schedule_sha256,NEW.schedules,NEW.fence,NEW.deadline_at,NEW.created_at)
        IS DISTINCT FROM ROW(OLD.job_id,OLD.checkpoint_id,OLD.boundary_kind,OLD.completed_update,
               OLD.schedule_sha256,OLD.schedules,OLD.fence,OLD.deadline_at,OLD.created_at) THEN
            RAISE EXCEPTION 'training evaluation obligation is immutable';
        END IF;
        IF OLD.group_id IS NOT NULL AND NEW.group_id IS DISTINCT FROM OLD.group_id THEN
            RAISE EXCEPTION 'training evaluation group binding is immutable';
        END IF;
        RETURN NEW; END; $$""")
    op.execute(
        "CREATE TRIGGER training_evaluation_trigger_immutable BEFORE UPDATE ON training_evaluation_triggers FOR EACH ROW EXECUTE FUNCTION protect_training_evaluation_trigger()"
    )
    op.execute("""CREATE FUNCTION protect_adapter_evaluation_purpose() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
        IF ROW(NEW.purpose,NEW.evaluation_trigger_id) IS DISTINCT FROM ROW(OLD.purpose,OLD.evaluation_trigger_id) THEN
            RAISE EXCEPTION 'adapter evaluation purpose is immutable';
        END IF;
        RETURN NEW; END; $$""")
    op.execute(
        "CREATE TRIGGER adapter_evaluation_purpose_immutable BEFORE UPDATE ON training_adapters FOR EACH ROW EXECUTE FUNCTION protect_adapter_evaluation_purpose()"
    )
    op.execute("""CREATE FUNCTION protect_evaluation_run_intent() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
        IF OLD.measurement_id IS NOT NULL AND NEW.measurement_id IS DISTINCT FROM OLD.measurement_id THEN
            RAISE EXCEPTION 'evaluation measurement binding is immutable';
        END IF;
        IF OLD.started_deadline_at IS NOT NULL AND NEW.started_deadline_at IS DISTINCT FROM OLD.started_deadline_at THEN
            RAISE EXCEPTION 'evaluation execution deadline is immutable after admission';
        END IF;
        IF ROW(NEW.group_id,NEW.owner_user_id,NEW.suite_row_id,NEW.suite_snapshot,NEW.subjects,NEW.data_snapshot,NEW.source_run_id,NEW.authorization_snapshot,NEW.request_sha256,NEW.idempotency_key_sha256,NEW.queue_deadline_at,NEW.execution_deadline_at,NEW.created_at)
        IS DISTINCT FROM ROW(OLD.group_id,OLD.owner_user_id,OLD.suite_row_id,OLD.suite_snapshot,OLD.subjects,OLD.data_snapshot,OLD.source_run_id,OLD.authorization_snapshot,OLD.request_sha256,OLD.idempotency_key_sha256,OLD.queue_deadline_at,OLD.execution_deadline_at,OLD.created_at) THEN
            RAISE EXCEPTION 'evaluation intent is immutable';
        END IF;
        RETURN NEW; END; $$""")
    op.execute(
        "CREATE TRIGGER evaluation_run_immutable BEFORE UPDATE ON evaluation_runs FOR EACH ROW EXECUTE FUNCTION protect_evaluation_run_intent()"
    )
    op.execute("""CREATE FUNCTION protect_evaluation_suite() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
        IF ROW(NEW.suite_id,NEW.version,NEW.definition,NEW.content_sha256,NEW.owner_user_id,NEW.attribution,NEW.created_at)
        IS DISTINCT FROM ROW(OLD.suite_id,OLD.version,OLD.definition,OLD.content_sha256,OLD.owner_user_id,OLD.attribution,OLD.created_at) THEN
            RAISE EXCEPTION 'evaluation suite definition is immutable';
        END IF;
        RETURN NEW; END; $$""")
    op.execute(
        "CREATE TRIGGER evaluation_suite_immutable BEFORE UPDATE ON evaluation_suites FOR EACH ROW EXECUTE FUNCTION protect_evaluation_suite()"
    )
    op.execute("""CREATE FUNCTION protect_evaluation_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'evaluation mutation receipt is immutable'; END; $$""")
    op.execute(
        "CREATE TRIGGER evaluation_mutation_immutable BEFORE UPDATE OR DELETE ON evaluation_mutations FOR EACH ROW EXECUTE FUNCTION protect_evaluation_mutation()"
    )
    op.execute("""CREATE FUNCTION protect_evaluation_measurement() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
        IF ROW(NEW.owner_user_id,NEW.request,NEW.authorization_snapshot,NEW.deadline_at,NEW.created_at)
        IS DISTINCT FROM ROW(OLD.owner_user_id,OLD.request,OLD.authorization_snapshot,OLD.deadline_at,OLD.created_at) THEN
            RAISE EXCEPTION 'evaluation measurement intent is immutable';
        END IF;
        IF OLD.report IS NOT NULL AND ROW(NEW.report,NEW.report_sha256,NEW.state,NEW.version)
        IS DISTINCT FROM ROW(OLD.report,OLD.report_sha256,OLD.state,OLD.version) THEN
            RAISE EXCEPTION 'evaluation measurement report is immutable';
        END IF;
        RETURN NEW; END; $$""")
    op.execute(
        "CREATE TRIGGER evaluation_measurement_immutable BEFORE UPDATE ON evaluation_measurements FOR EACH ROW EXECUTE FUNCTION protect_evaluation_measurement()"
    )
    op.execute("""CREATE FUNCTION protect_evaluation_profile() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
        IF ROW(NEW.measurement_id,NEW.fingerprint_sha256,NEW.report_sha256,NEW.expires_at)
        IS DISTINCT FROM ROW(OLD.measurement_id,OLD.fingerprint_sha256,OLD.report_sha256,OLD.expires_at) THEN
            RAISE EXCEPTION 'evaluation profile evidence is immutable';
        END IF;
        RETURN NEW; END; $$""")
    op.execute(
        "CREATE TRIGGER evaluation_profile_immutable BEFORE UPDATE ON evaluation_coexistence_profiles FOR EACH ROW EXECUTE FUNCTION protect_evaluation_profile()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM evaluation_runs WHERE state NOT IN ('succeeded','failed','timed_out','cancelled') OR cleanup_state <> 'complete') OR EXISTS (SELECT 1 FROM evaluation_checkpoint_pins WHERE released_at IS NULL) OR EXISTS (SELECT 1 FROM training_evaluation_triggers WHERE phase <> 'complete') OR EXISTS (SELECT 1 FROM evaluation_measurements WHERE state IN ('queued','running'))"
        )
    ).scalar():
        raise RuntimeError("drain live evaluations and checkpoint pins before downgrade")
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM evaluation_results)")).scalar():
        raise RuntimeError(
            "retained evaluation history prevents destructive downgrade; use drained binary rollback"
        )
    op.execute("DROP TRIGGER evaluation_result_fence ON evaluation_results")
    op.execute("DROP TRIGGER training_evaluation_trigger_immutable ON training_evaluation_triggers")
    op.execute("DROP FUNCTION protect_training_evaluation_trigger()")
    op.execute("DROP TRIGGER adapter_evaluation_purpose_immutable ON training_adapters")
    op.execute("DROP FUNCTION protect_adapter_evaluation_purpose()")
    op.execute("DROP TRIGGER evaluation_group_immutable ON evaluation_groups")
    op.execute("DROP FUNCTION protect_evaluation_group_intent()")
    op.execute("DROP TRIGGER evaluation_mutation_immutable ON evaluation_mutations")
    op.execute("DROP FUNCTION protect_evaluation_mutation()")
    op.execute("DROP TRIGGER evaluation_measurement_immutable ON evaluation_measurements")
    op.execute("DROP FUNCTION protect_evaluation_measurement()")
    op.execute("DROP TRIGGER evaluation_profile_immutable ON evaluation_coexistence_profiles")
    op.execute("DROP FUNCTION protect_evaluation_profile()")
    op.execute("DROP FUNCTION fence_evaluation_result()")
    op.execute("DROP TRIGGER evaluation_run_immutable ON evaluation_runs")
    op.execute("DROP FUNCTION protect_evaluation_run_intent()")
    op.execute("DROP TRIGGER evaluation_result_immutable ON evaluation_results")
    op.execute("DROP FUNCTION protect_evaluation_result()")
    op.execute("DROP TRIGGER evaluation_suite_immutable ON evaluation_suites")
    op.execute("DROP FUNCTION protect_evaluation_suite()")
    for table, column in (
        ("training_adapters", "evaluation_trigger_id"),
        ("training_adapters", "purpose"),
        ("training_jobs", "evaluation_pause_trigger_id"),
        ("agent_runs", "evaluation_attempt_id"),
        ("agent_runs", "purpose"),
    ):
        op.drop_column(table, column)
    for name in reversed(TABLE_ORDER):
        op.drop_table(name)
