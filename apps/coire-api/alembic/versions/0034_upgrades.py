"""Durable upgrade authority and admission fences; never delete retained history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from coire_api.upgrades.recovery_schema_sql import RECOVERY_IMMUTABILITY_SQL

revision = "0034_upgrades"
down_revision = "0033_preference_feedback"
branch_labels = None
depends_on = None

TABLE_ORDER = (
    "upgrade_releases",
    "upgrade_operations",
    "upgrade_maintenance",
    "upgrade_receipts",
    "upgrade_executors",
    "upgrade_activation_grants",
    "upgrade_handovers",
    "upgrade_expired_recoveries",
    "upgrade_recovery_successions",
    "upgrade_recovery_successor_claims",
)
UPGRADE_SQL: tuple[str, ...] = (
    "\nCREATE TABLE upgrade_releases (\n\tid UUID NOT NULL, \n\tmanifest_sha256 VARCHAR(64) NOT NULL, \n\tprepared_artifact_id VARCHAR(128) NOT NULL, \n\tprepared_by UUID NOT NULL, \n\tpayload JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (manifest_sha256), \n\tUNIQUE (prepared_artifact_id), \n\tFOREIGN KEY(prepared_by) REFERENCES users (id) ON DELETE RESTRICT\n)\n\n",
    "\nCREATE TABLE upgrade_operations (\n\tid VARCHAR(26) NOT NULL, \n\tuser_id UUID NOT NULL, \n\tkey_id UUID, \n\tcredential_version INTEGER, \n\tsource_release_id UUID NOT NULL, \n\ttarget_release_id UUID NOT NULL, \n\tidempotency_key UUID NOT NULL, \n\trequest_sha256 VARCHAR(64) NOT NULL, \n\tgeneration BIGINT NOT NULL, \n\tactive_slot INTEGER, \n\tphase VARCHAR(32) NOT NULL, \n\tpayload JSONB NOT NULL, \n\texecution_plan JSONB, \n\tnode_command JSONB, \n\tmigration_execution JSONB, \n\texecutor_identity VARCHAR(128), \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_upgrade_request UNIQUE (user_id, idempotency_key), \n\tCONSTRAINT ck_upgrade_owner CHECK (generation >= 1 AND (active_slot IS NULL OR active_slot = 1)), \n\tCONSTRAINT ck_upgrade_phase CHECK (phase IN ('accepted','preflight','fencing','draining','staged','smoke_before','migrating','activating','verifying','qualifying','committing','restoring','verifying_source','recovery_required','succeeded','failed_unchanged','cancelled_unchanged','rolled_back')), \n\tCONSTRAINT ck_upgrade_terminal_owner CHECK ((active_slot IS NULL) = (phase IN ('succeeded','failed_unchanged','cancelled_unchanged','rolled_back'))), \n\tFOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(key_id) REFERENCES api_keys (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(source_release_id) REFERENCES upgrade_releases (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(target_release_id) REFERENCES upgrade_releases (id) ON DELETE RESTRICT, \n\tUNIQUE (generation), \n\tUNIQUE (active_slot)\n)\n\n",
    "\nCREATE TABLE upgrade_maintenance (\n\ttarget VARCHAR(64) NOT NULL, \n\tjob_id VARCHAR(26) NOT NULL, \n\tgeneration BIGINT NOT NULL, \n\treleased BOOLEAN NOT NULL, \n\tpayload JSONB NOT NULL, \n\tPRIMARY KEY (target), \n\tCONSTRAINT ck_upgrade_maintenance_generation CHECK (generation >= 1), \n\tFOREIGN KEY(job_id) REFERENCES upgrade_operations (id) ON DELETE RESTRICT\n)\n\n",
    "\nCREATE TABLE upgrade_receipts (\n\tjob_id VARCHAR(26) NOT NULL, \n\tgeneration BIGINT NOT NULL, \n\tsequence BIGINT NOT NULL, \n\treceipt_sha256 VARCHAR(64) NOT NULL, \n\tpayload JSONB NOT NULL, \n\tPRIMARY KEY (job_id, generation, sequence), \n\tCONSTRAINT ck_upgrade_receipt_sequence CHECK (generation >= 1 AND sequence >= 1), \n\tFOREIGN KEY(job_id) REFERENCES upgrade_operations (id) ON DELETE RESTRICT\n)\n\n",
    "\nCREATE TABLE upgrade_executors (\n\tproject VARCHAR(63) NOT NULL, \n\tidentity VARCHAR(128) NOT NULL, \n\timage_id VARCHAR(71) NOT NULL, \n\tsource_release_id UUID NOT NULL, \n\tbootstrap_ready BOOLEAN NOT NULL, \n\tbootstrap_payload JSONB, \n\theartbeat_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tgeneration BIGINT NOT NULL, \n\tPRIMARY KEY (project), \n\tCONSTRAINT ck_upgrade_executor_generation CHECK (generation >= 1), \n\tFOREIGN KEY(source_release_id) REFERENCES upgrade_releases (id) ON DELETE RESTRICT\n)\n\n",
    "\nCREATE TABLE upgrade_activation_grants (\n\tid UUID NOT NULL, \n\tjob_id VARCHAR(26) NOT NULL, \n\tgeneration BIGINT NOT NULL, \n\tboundary VARCHAR(16) NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tconsumed_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_upgrade_activation_boundary UNIQUE (job_id, generation, boundary), \n\tCONSTRAINT ck_upgrade_activation_boundary CHECK (generation >= 1 AND boundary IN ('migration','runtime')), \n\tFOREIGN KEY(job_id) REFERENCES upgrade_operations (id) ON DELETE RESTRICT\n)\n\n",
    "\nCREATE TABLE upgrade_handovers (\n\tid UUID NOT NULL, \n\tproject VARCHAR(63) NOT NULL, \n\tuser_id UUID NOT NULL, \n\tkey_id UUID, \n\tcredential_version INTEGER, \n\tidempotency_key UUID NOT NULL, \n\tpreparation_sha256 VARCHAR(64) NOT NULL, \n\tgeneration BIGINT NOT NULL, \n\tactive_slot INTEGER, \n\tphase VARCHAR(32) NOT NULL, \n\tpreparation_payload JSONB NOT NULL, \n\ttransfer_payload JSONB, \n\tcommit_payload JSONB, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_upgrade_handover_request UNIQUE (user_id, idempotency_key), \n\tCONSTRAINT ck_upgrade_handover_generation CHECK (generation >= 1), \n\tCONSTRAINT ck_upgrade_handover_slot CHECK (active_slot IS NULL OR active_slot = 1), \n\tCONSTRAINT ck_upgrade_handover_phase CHECK (phase IN ('accepted','passive','verified','transfer_consumed','claimed','stable','committing','committed','restoring','restored','failed_unchanged')), \n\tCONSTRAINT ck_upgrade_handover_terminal CHECK ((active_slot IS NULL) = (phase IN ('committed','restored','failed_unchanged'))), \n\tCONSTRAINT ck_upgrade_handover_transfer_required CHECK (phase NOT IN ('transfer_consumed','claimed','stable','committing','committed') OR transfer_payload IS NOT NULL), \n\tCONSTRAINT ck_upgrade_handover_transfer_retained CHECK (transfer_payload IS NULL OR phase IN ('transfer_consumed','claimed','stable','committing','committed','restoring','restored')), \n\tCONSTRAINT ck_upgrade_handover_commit_required CHECK (phase NOT IN ('committing','committed') OR commit_payload IS NOT NULL), \n\tCONSTRAINT ck_upgrade_handover_commit_retained CHECK (commit_payload IS NULL OR phase IN ('committing','committed','restoring','restored')), \n\tFOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(key_id) REFERENCES api_keys (id) ON DELETE RESTRICT, \n\tUNIQUE (active_slot)\n)\n\n",
)


UPGRADE_SQL += (
    """CREATE TABLE upgrade_expired_recoveries (
    id VARCHAR(26) PRIMARY KEY,
    original_job_id VARCHAR(26) NOT NULL REFERENCES upgrade_operations(id) ON DELETE RESTRICT,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    key_id UUID REFERENCES api_keys(id) ON DELETE RESTRICT,
    credential_version INTEGER,
    idempotency_key VARCHAR(128) NOT NULL,
    request_sha256 VARCHAR(64) NOT NULL,
    intent_sha256 VARCHAR(64) NOT NULL,
    intent_payload JSONB NOT NULL,
    active_slot INTEGER UNIQUE,
    phase VARCHAR(32) NOT NULL,
    transfer_payload JSONB,
    consumption_payload JSONB,
    owner_claim_payload JSONB,
    cold_claim_payload JSONB,
    cold_smoke_payload JSONB,
    data_baseline_payload JSONB,
    native_baseline_payload JSONB,
    cold_verification_payload JSONB,
    cold_handback_intent_payload JSONB,
    cold_handback_receipt_payload JSONB,
    cancellation_payload JSONB,
    resolution_payload JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT uq_expired_recovery_request UNIQUE(user_id,idempotency_key),
    CONSTRAINT ck_expired_recovery_distinct_id CHECK(id != original_job_id),
    CONSTRAINT ck_expired_recovery_slot CHECK(active_slot IS NULL OR active_slot = 1),
    CONSTRAINT ck_expired_recovery_phase CHECK(phase IN ('accepted','transferring','transfer_consumed','recovering','verifying','cold_recovering','handback_prepared','handing_back','resolving','recovery_required','recovered','cancelled_unchanged','superseded')),
    CONSTRAINT ck_expired_recovery_terminal_owner CHECK((active_slot IS NULL) = (phase IN ('recovered','cancelled_unchanged','superseded'))),
    CONSTRAINT ck_cold_recovery_supersession_payload CHECK(phase <> 'superseded' OR (cancellation_payload IS NOT NULL AND resolution_payload IS NOT NULL AND (cold_claim_payload IS NOT NULL OR coalesce((resolution_payload->>'protocol' = 'cold-unstarted-supersession-v1' AND resolution_payload->'unstarted_startup'->'intent' = intent_payload AND transfer_payload IS NULL AND consumption_payload IS NULL AND owner_claim_payload IS NULL AND cold_claim_payload IS NULL AND cold_smoke_payload IS NULL AND cold_verification_payload IS NULL AND cold_handback_intent_payload IS NULL AND cold_handback_receipt_payload IS NULL),false)))),
    CONSTRAINT ck_expired_recovery_transfer_required CHECK(phase NOT IN ('transferring','transfer_consumed','recovering','verifying','recovered') OR transfer_payload IS NOT NULL OR (phase = 'recovered' AND cold_claim_payload IS NOT NULL)),
    CONSTRAINT ck_expired_recovery_consumption_required CHECK(phase NOT IN ('transfer_consumed','recovering','verifying','recovered') OR consumption_payload IS NOT NULL OR (phase = 'recovered' AND cold_claim_payload IS NOT NULL)),
    CONSTRAINT ck_expired_recovery_owner_required CHECK(phase NOT IN ('recovering','verifying','recovered') OR owner_claim_payload IS NOT NULL OR (phase = 'recovered' AND cold_claim_payload IS NOT NULL)),
    CONSTRAINT ck_expired_recovery_cold_owner_required CHECK(phase != 'cold_recovering' OR cold_claim_payload IS NOT NULL),
    CONSTRAINT ck_expired_recovery_cold_smoke_owner CHECK(cold_smoke_payload IS NULL OR cold_claim_payload IS NOT NULL),
    CONSTRAINT ck_expired_recovery_cold_verification CHECK(cold_verification_payload IS NULL OR (cold_smoke_payload IS NOT NULL AND data_baseline_payload IS NOT NULL AND native_baseline_payload IS NOT NULL)),
    CONSTRAINT ck_expired_recovery_handback_verified CHECK(phase NOT IN ('handback_prepared','handing_back','resolving') OR cold_verification_payload IS NOT NULL),
    CONSTRAINT ck_expired_recovery_handback_intent CHECK(phase NOT IN ('handing_back','resolving') OR cold_handback_intent_payload IS NOT NULL),
    CONSTRAINT ck_expired_recovery_handback_receipt CHECK(cold_handback_receipt_payload IS NULL OR cold_handback_intent_payload IS NOT NULL),
    CONSTRAINT ck_expired_recovery_cold_distinct CHECK(cold_claim_payload IS NULL OR (transfer_payload IS NULL AND consumption_payload IS NULL AND owner_claim_payload IS NULL AND COALESCE(intent_payload->'request'->>'recovery_mode' = 'cold', false))),
    CONSTRAINT ck_expired_recovery_owner_consumption CHECK(owner_claim_payload IS NULL OR consumption_payload IS NOT NULL),
    CONSTRAINT ck_expired_recovery_cancel_retained CHECK(cancellation_payload IS NULL OR phase IN ('recovery_required','recovered','superseded','cancelled_unchanged')),
    CONSTRAINT ck_unstarted_recovery_no_effect_authority CHECK(phase <> 'cancelled_unchanged' OR (transfer_payload IS NULL AND consumption_payload IS NULL AND owner_claim_payload IS NULL AND cold_claim_payload IS NULL AND cold_smoke_payload IS NULL AND cold_verification_payload IS NULL AND cold_handback_intent_payload IS NULL AND cold_handback_receipt_payload IS NULL AND resolution_payload IS NULL)),
    CONSTRAINT ck_expired_recovery_resolution_required CHECK(phase != 'recovered' OR resolution_payload IS NOT NULL)
)""",
)


UPGRADE_SQL += (
    """CREATE TABLE upgrade_recovery_successions (
    id VARCHAR(26) PRIMARY KEY,
    recovery_id VARCHAR(26) NOT NULL REFERENCES upgrade_expired_recoveries(id) ON DELETE RESTRICT,
    predecessor_sha256 VARCHAR(64) NOT NULL,
    grant_sha256 VARCHAR(64) NOT NULL,
    grant_payload JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT uq_recovery_succession_predecessor UNIQUE(recovery_id,predecessor_sha256),
    CONSTRAINT ck_recovery_succession_distinct_id CHECK(id != recovery_id)
)""",
)


UPGRADE_SQL += (
    """CREATE TABLE upgrade_recovery_successor_claims (
    id VARCHAR(26) PRIMARY KEY REFERENCES upgrade_recovery_successions(id) ON DELETE RESTRICT,
    claim_sha256 VARCHAR(64) NOT NULL,
    claim_payload JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL
)""",
)


def upgrade() -> None:
    op.add_column("harness_evaluations", sa.Column("harness_image", sa.String(512), nullable=True))
    op.add_column("harness_evaluations", sa.Column("runtime_sha256", sa.String(64), nullable=True))
    op.add_column("model_instances", sa.Column("upgrade_smoke_payload", JSONB(), nullable=True))
    op.add_column("model_instances", sa.Column("upgrade_smoke_result", JSONB(), nullable=True))
    op.add_column("nodes", sa.Column("runtime_sha256", sa.String(64), nullable=True))
    for endpoint in ("a", "b"):
        op.add_column(
            "link_observations",
            sa.Column(f"runtime_sha256_{endpoint}", sa.String(64), nullable=True),
        )
    op.execute(sa.text("CREATE SEQUENCE upgrade_generation_seq AS BIGINT"))
    for statement in UPGRADE_SQL:
        op.execute(sa.text(statement))
    op.execute(
        sa.text("""CREATE FUNCTION upgrade_immutable_history() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'upgrade history is immutable'; END $$""")
    )
    for table in ("upgrade_releases", "upgrade_receipts"):
        op.execute(
            sa.text(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION upgrade_immutable_history()"
            )
        )
    op.execute(
        sa.text("""CREATE FUNCTION upgrade_handover_immutable_authority()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'handover authority is immutable';
          END IF;
          IF ROW(OLD.id, OLD.project, OLD.user_id, OLD.key_id,
                 OLD.credential_version, OLD.idempotency_key,
                 OLD.preparation_sha256, OLD.generation,
                 OLD.preparation_payload, OLD.created_at)
             IS DISTINCT FROM
             ROW(NEW.id, NEW.project, NEW.user_id, NEW.key_id,
                 NEW.credential_version, NEW.idempotency_key,
                 NEW.preparation_sha256, NEW.generation,
                 NEW.preparation_payload, NEW.created_at) THEN
            RAISE EXCEPTION 'handover authority is immutable';
          END IF;
          IF OLD.transfer_payload IS NOT NULL AND
             OLD.transfer_payload IS DISTINCT FROM NEW.transfer_payload THEN
            RAISE EXCEPTION 'handover transfer intent is immutable';
          END IF;
          IF OLD.commit_payload IS NOT NULL AND
             OLD.commit_payload IS DISTINCT FROM NEW.commit_payload THEN
            RAISE EXCEPTION 'handover commit decision is immutable';
          END IF;
          RETURN NEW;
        END $$""")
    )
    op.execute(
        sa.text("""CREATE TRIGGER upgrade_handovers_authority
        BEFORE UPDATE OR DELETE ON upgrade_handovers FOR EACH ROW
        EXECUTE FUNCTION upgrade_handover_immutable_authority()""")
    )
    for statement in RECOVERY_IMMUTABILITY_SQL:
        op.execute(sa.text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM harness_evaluations WHERE runtime_sha256 IS NOT NULL OR harness_image IS NOT NULL)"
        )
    ):
        raise RuntimeError("upgrade downgrade refuses retained harness qualification history")
    if bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM model_instances WHERE upgrade_smoke_payload IS NOT NULL OR upgrade_smoke_result IS NOT NULL)"
        )
    ):
        raise RuntimeError("upgrade downgrade refuses retained serving smoke ownership")
    if any(bind.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {name})")) for name in TABLE_ORDER):
        raise RuntimeError("upgrade downgrade refuses retained operational or audit history")
    if bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM link_observations WHERE runtime_sha256_a IS NOT NULL OR runtime_sha256_b IS NOT NULL)"
        )
    ):
        raise RuntimeError("upgrade downgrade refuses retained qualification history")
    for name in reversed(TABLE_ORDER):
        op.drop_table(name)
    op.execute(sa.text("DROP FUNCTION recovery_successor_immutable_claim()"))
    op.execute(sa.text("DROP FUNCTION recovery_succession_immutable_grant()"))
    op.execute(sa.text("DROP FUNCTION expired_recovery_immutable_authority()"))
    op.execute(sa.text("DROP FUNCTION upgrade_handover_immutable_authority()"))
    op.execute(sa.text("DROP FUNCTION upgrade_immutable_history()"))
    op.execute(sa.text("DROP SEQUENCE upgrade_generation_seq"))
    for endpoint in ("a", "b"):
        op.drop_column("link_observations", f"runtime_sha256_{endpoint}")
    op.drop_column("nodes", "runtime_sha256")
    op.drop_column("model_instances", "upgrade_smoke_payload")
    op.drop_column("model_instances", "upgrade_smoke_result")
    op.drop_column("harness_evaluations", "runtime_sha256")
    op.drop_column("harness_evaluations", "harness_image")
