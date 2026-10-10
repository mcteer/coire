"""Preference feedback privacy and immutable lineage. Frozen additive SQL."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0033_preference_feedback"
down_revision: str | None = "0032_evaluation_verbs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_ORDER = (
    "feedback_preferences",
    "chat_feedback_provenance",
    "comparison_pairs",
    "chat_active_answers",
    "feedback_rows",
    "feedback_mutations",
    "feedback_review_skips",
    "preference_exports",
    "preference_export_members",
    "adapter_lineage",
)
UPGRADE_SQL = (
    "CREATE TABLE feedback_preferences (\n\towner_user_id UUID NOT NULL, \n\tenabled BOOLEAN DEFAULT true NOT NULL, \n\tcapture_generation BIGINT DEFAULT 1 NOT NULL, \n\tversion INTEGER DEFAULT 1 NOT NULL, \n\tdisclosure_version VARCHAR(32) DEFAULT 'feedback-v1' NOT NULL, \n\tchanged_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\twithdrawn_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (owner_user_id), \n\tCONSTRAINT ck_feedback_generation CHECK (capture_generation >= 1 AND version >= 1), \n\tFOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT\n)",
    "CREATE TABLE chat_feedback_provenance (\n\tid UUID NOT NULL, \n\towner_user_id UUID NOT NULL, \n\tconversation_id UUID NOT NULL, \n\tsource_turn_id UUID NOT NULL, \n\tsource_message_id UUID NOT NULL, \n\tcapture_generation BIGINT NOT NULL, \n\tcontext_revision BIGINT NOT NULL, \n\ttarget JSONB NOT NULL, \n\tsettings JSONB NOT NULL, \n\ttokenizer_sha256 VARCHAR(64) NOT NULL, \n\ttemplate_sha256 VARCHAR(64) NOT NULL, \n\tprompt_sha256 VARCHAR(64) NOT NULL, \n\tprompt JSONB, \n\tcounted_bytes BIGINT DEFAULT 0 NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\twithdrawn_at TIMESTAMP WITH TIME ZONE, \n\tpurge_after TIMESTAMP WITH TIME ZONE, \n\tpurged_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_feedback_provenance_turn UNIQUE (source_turn_id), \n\tCONSTRAINT ck_feedback_provenance_bounds CHECK (capture_generation >= 1 AND context_revision >= 1 AND counted_bytes >= 0), \n\tFOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT\n)",
    "CREATE INDEX ix_chat_feedback_provenance_conversation_id ON chat_feedback_provenance (conversation_id)",
    "CREATE INDEX ix_feedback_provenance_cleanup ON chat_feedback_provenance (purge_after, id)",
    "CREATE INDEX ix_feedback_provenance_owner ON chat_feedback_provenance (owner_user_id, capture_generation)",
    "CREATE TABLE comparison_pairs (\n\tid VARCHAR(26) NOT NULL, \n\towner_user_id UUID NOT NULL, \n\tconversation_id UUID NOT NULL, \n\tsource_turn_id UUID NOT NULL, \n\tsource_message_id UUID NOT NULL, \n\tprovenance_id UUID, \n\tcandidate_message_id UUID, \n\tcapture_generation BIGINT NOT NULL, \n\tcontext_revision BIGINT NOT NULL, \n\tclient_request_id UUID NOT NULL, \n\trequest_sha256 VARCHAR(64) NOT NULL, \n\ttarget JSONB, \n\tprompt JSONB, \n\toriginal TEXT, \n\tcandidate TEXT, \n\tgeneration_state VARCHAR(16) DEFAULT 'queued' NOT NULL, \n\tselection_state VARCHAR(16) DEFAULT 'pending' NOT NULL, \n\tselected_candidate VARCHAR(16), \n\tversion INTEGER DEFAULT 1 NOT NULL, \n\tcounted_bytes BIGINT DEFAULT 0 NOT NULL, \n\texecution JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\twithdrawn_at TIMESTAMP WITH TIME ZONE, \n\tpurge_after TIMESTAMP WITH TIME ZONE, \n\tpurged_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_comparison_request UNIQUE (owner_user_id, client_request_id), \n\tCONSTRAINT ck_comparison_bounds CHECK (capture_generation >= 1 AND context_revision >= 1 AND version >= 1 AND counted_bytes BETWEEN 0 AND 262144), \n\tCONSTRAINT ck_comparison_states CHECK (generation_state IN ('queued','running','ready','failed','cancelled','identical','expired','withdrawn') AND selection_state IN ('pending','chosen','dismissed','expired','withdrawn')), \n\tCONSTRAINT ck_comparison_choice CHECK (selected_candidate IS NULL OR selected_candidate IN ('original','candidate')), \n\tFOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(provenance_id) REFERENCES chat_feedback_provenance (id) ON DELETE SET NULL\n)",
    "CREATE INDEX ix_comparison_owner ON comparison_pairs (owner_user_id, capture_generation)",
    "CREATE INDEX ix_comparison_purge ON comparison_pairs (purge_after, id)",
    "CREATE INDEX ix_comparison_review ON comparison_pairs (created_at, id)",
    "CREATE UNIQUE INDEX uq_comparison_pending ON comparison_pairs (conversation_id) WHERE selection_state = 'pending'",
    "CREATE TABLE chat_active_answers (\n\tsource_turn_id UUID NOT NULL, \n\tconversation_id UUID NOT NULL, \n\tselected_message_id UUID NOT NULL, \n\tselection_revision BIGINT NOT NULL, \n\tPRIMARY KEY (source_turn_id)\n)",
    "CREATE INDEX ix_chat_active_answers_conversation_id ON chat_active_answers (conversation_id)",
    "CREATE TABLE feedback_rows (\n\tid UUID NOT NULL, \n\towner_user_id UUID NOT NULL, \n\tactor_user_id UUID NOT NULL, \n\tconversation_id UUID NOT NULL, \n\tmessage_id UUID, \n\tpair_id VARCHAR(26), \n\tkind VARCHAR(8) NOT NULL, \n\tsource VARCHAR(8) NOT NULL, \n\tjudgement VARCHAR(16), \n\ttags JSONB DEFAULT '[]'::jsonb NOT NULL, \n\tcapture_generation BIGINT NOT NULL, \n\tversion INTEGER DEFAULT 1 NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\twithdrawn_at TIMESTAMP WITH TIME ZONE, \n\tpurged_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_feedback_row_kind CHECK (kind IN ('thumb','pair') AND source IN ('owner','admin') AND version >= 1 AND capture_generation >= 1), \n\tCONSTRAINT ck_feedback_row_subject CHECK ((kind='thumb' AND message_id IS NOT NULL AND pair_id IS NULL AND source='owner' AND (judgement IS NULL OR judgement IN ('up','down'))) OR (kind='pair' AND message_id IS NULL AND pair_id IS NOT NULL AND ((judgement IS NOT NULL AND judgement IN ('original','candidate')) OR (withdrawn_at IS NOT NULL AND judgement IS NULL)))), \n\tFOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_user_id) REFERENCES users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(pair_id) REFERENCES comparison_pairs (id) ON DELETE RESTRICT\n)",
    "CREATE INDEX ix_feedback_owner ON feedback_rows (owner_user_id, capture_generation)",
    "CREATE UNIQUE INDEX uq_feedback_pair_source ON feedback_rows (pair_id, source) WHERE kind='pair'",
    "CREATE UNIQUE INDEX uq_feedback_thumb ON feedback_rows (owner_user_id, message_id) WHERE kind='thumb'",
    "CREATE TABLE feedback_mutations (\n\tid UUID NOT NULL, \n\tactor_user_id UUID NOT NULL, \n\toperation VARCHAR(128) NOT NULL, \n\trequest_id VARCHAR(128) NOT NULL, \n\tintent_sha256 VARCHAR(64) NOT NULL, \n\treceipt JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_feedback_mutation UNIQUE (actor_user_id, operation, request_id), \n\tFOREIGN KEY(actor_user_id) REFERENCES users (id) ON DELETE RESTRICT\n)",
    "CREATE TABLE feedback_review_skips (\n\tactor_user_id UUID NOT NULL, \n\tpair_id VARCHAR(26) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (actor_user_id, pair_id), \n\tFOREIGN KEY(actor_user_id) REFERENCES users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(pair_id) REFERENCES comparison_pairs (id) ON DELETE CASCADE\n)",
    "CREATE TABLE preference_exports (\n\tid VARCHAR(26) NOT NULL, \n\towner_user_id UUID NOT NULL, \n\tauthorization_snapshot JSONB NOT NULL, \n\trequest JSONB NOT NULL, \n\tstate VARCHAR(16) DEFAULT 'queued' NOT NULL, \n\tactive_slot INTEGER DEFAULT 1 NOT NULL, \n\tversion INTEGER DEFAULT 1 NOT NULL, \n\tfence BIGINT DEFAULT 0 NOT NULL, \n\tselected_count INTEGER DEFAULT 0 NOT NULL, \n\texcluded_count INTEGER DEFAULT 0 NOT NULL, \n\twarnings JSONB DEFAULT '[]'::jsonb NOT NULL, \n\tdataset_id UUID, \n\tstaging JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tcleanup_pending BOOLEAN DEFAULT false NOT NULL, \n\tterminal_reason VARCHAR(32), \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tqueue_deadline_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\texecution_deadline_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_preference_export_bounds CHECK (state IN ('queued','staging','publishing','succeeded','failed','cancelled') AND version >= 1 AND active_slot = 1 AND selected_count BETWEEN 0 AND 10000), \n\tFOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT\n)",
    "CREATE INDEX ix_preference_export_queue ON preference_exports (state, created_at, id)",
    "CREATE UNIQUE INDEX uq_preference_export_active ON preference_exports (active_slot) WHERE state IN ('staging','publishing')",
    "CREATE TABLE preference_export_members (\n\texport_id VARCHAR(26) NOT NULL, \n\tpair_id VARCHAR(26) NOT NULL, \n\towner_user_id UUID NOT NULL, \n\tcapture_generation BIGINT NOT NULL, \n\tconversation_id UUID NOT NULL, \n\tsource_message_id UUID NOT NULL, \n\tjudgement_source VARCHAR(8) NOT NULL, \n\tjudgement_version INTEGER NOT NULL, \n\tactor_user_id UUID NOT NULL, \n\ttarget JSONB NOT NULL, \n\tprompt_sha256 VARCHAR(64) NOT NULL, \n\tcontent_sha256 VARCHAR(64) NOT NULL, \n\trow_index INTEGER NOT NULL, \n\tpublished BOOLEAN DEFAULT false NOT NULL, \n\tPRIMARY KEY (export_id, pair_id), \n\tFOREIGN KEY(export_id) REFERENCES preference_exports (id) ON DELETE RESTRICT\n)",
    "CREATE TABLE adapter_lineage (\n\tadapter_id UUID NOT NULL, \n\tparent_adapter_id UUID, \n\tlineage JSONB NOT NULL, \n\tdepth INTEGER NOT NULL, \n\tPRIMARY KEY (adapter_id), \n\tCONSTRAINT ck_adapter_lineage_depth CHECK (depth BETWEEN 0 AND 32 AND adapter_id IS DISTINCT FROM parent_adapter_id), \n\tFOREIGN KEY(adapter_id) REFERENCES training_adapters (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(parent_adapter_id) REFERENCES training_adapters (id) ON DELETE RESTRICT\n)",
)

IMMUTABILITY_SQL = (
    """CREATE FUNCTION preference_immutable_history() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_TABLE_NAME = 'adapter_lineage' THEN
            RAISE EXCEPTION 'adapter lineage is immutable';
        END IF;
        IF OLD.published THEN
            RAISE EXCEPTION 'published preference history is immutable';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$""",
    """CREATE TRIGGER preference_member_immutable BEFORE UPDATE OR DELETE
       ON preference_export_members FOR EACH ROW EXECUTE FUNCTION preference_immutable_history()""",
    """CREATE TRIGGER adapter_lineage_immutable BEFORE UPDATE OR DELETE
       ON adapter_lineage FOR EACH ROW EXECUTE FUNCTION preference_immutable_history()""",
)


def upgrade() -> None:
    op.add_column(
        "chat_conversations",
        sa.Column("context_revision", sa.BigInteger(), server_default="1", nullable=False),
    )
    op.add_column("engine_processes", sa.Column("rendering_identity", JSONB(), nullable=True))
    for statement in (*UPGRADE_SQL, *IMMUTABILITY_SQL):
        op.execute(sa.text(statement))
    op.add_column(
        "chat_feedback_provenance", sa.Column("runtime_sha256", sa.String(64), nullable=False)
    )
    op.add_column(
        "comparison_pairs",
        sa.Column("accounting", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.add_column(
        "preference_exports",
        sa.Column("matched_count", sa.BigInteger(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    retained = any(
        bind.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {name})")) for name in TABLE_ORDER
    )
    retained = retained or bool(
        bind.scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM training_jobs WHERE submitted_spec->>'schema_version'='3')"
            )
        )
    )
    if retained:
        raise RuntimeError("preference downgrade refuses retained feedback, lineage or v3 history")
    for name in reversed(TABLE_ORDER):
        op.drop_table(name)
    op.execute(sa.text("DROP FUNCTION preference_immutable_history()"))
    op.drop_column("engine_processes", "rendering_identity")
    op.drop_column("chat_conversations", "context_revision")
