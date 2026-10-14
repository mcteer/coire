"""Separate recovery persistence retains original operation and idempotency."""

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable


def test_recovery_schema_has_separate_authority_and_one_active_owner():
    from coire_api.db import UpgradeExpiredRecoveryRow

    table = UpgradeExpiredRecoveryRow.__table__
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert table.name == "upgrade_expired_recoveries"
    assert (
        "FOREIGN KEY(original_job_id) REFERENCES upgrade_operations (id) ON DELETE RESTRICT" in ddl
    )
    assert "UNIQUE (user_id, idempotency_key)" in ddl
    assert "UNIQUE (active_slot)" in ddl
    assert "intent_payload JSONB NOT NULL" in ddl
    assert "intent_sha256 VARCHAR(64) NOT NULL" in ddl
    assert "ck_expired_recovery_terminal_owner" in ddl
    assert "resolution_payload JSONB" in ddl
    assert "consumption_payload JSONB" in ddl
    assert "owner_claim_payload JSONB" in ddl
    assert "ck_expired_recovery_owner_required" in ddl
    assert "ck_expired_recovery_owner_consumption" in ddl
    assert "ck_expired_recovery_consumption_required" in ddl
    assert "original_recovery_deadline_at" not in table.c  # Inside immutable captured intent.
