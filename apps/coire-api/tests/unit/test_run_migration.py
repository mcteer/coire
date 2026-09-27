from pathlib import Path


def test_container_run_migration_is_reversible_and_server_revocable() -> None:
    source = Path("apps/coire-api/alembic/versions/0011_container_runs.py").read_text()
    assert 'revision: str = "0011_container_runs"' in source
    assert 'down_revision: str | None = "0010_harness_evaluations"' in source
    for table in ("agent_runs", "agent_run_transitions", "run_tokens", "run_commands"):
        assert f'"{table}"' in source
        assert f'op.drop_table("{table}")' in source
    for column in ("prefix", "secret_hash", "expires_at", "revoked_at", "spent_tokens"):
        assert f'"{column}"' in source
    assert "secret" + "_plaintext" not in source
