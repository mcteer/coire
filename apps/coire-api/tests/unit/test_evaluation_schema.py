"""Durable evaluation identity and history constraints."""

from sqlalchemy import UniqueConstraint

from coire_api.db import Base


def test_evaluation_history_and_execution_tables_exist() -> None:
    expected = {
        "evaluation_suites",
        "evaluation_groups",
        "evaluation_runs",
        "evaluation_attempts",
        "evaluation_results",
        "evaluation_evidence",
        "training_evaluation_triggers",
        "evaluation_checkpoint_pins",
        "evaluation_events",
        "evaluation_measurements",
        "evaluation_coexistence_profiles",
    }
    assert expected <= set(Base.metadata.tables)
    for name in expected:
        assert Base.metadata.tables[name].primary_key.columns


def test_execution_and_terminal_identity_cannot_duplicate() -> None:
    identities: dict[str, set[tuple[str, ...]]] = {
        "evaluation_suites": {("suite_id", "version")},
        "evaluation_runs": {("owner_user_id", "idempotency_key_sha256")},
        "evaluation_attempts": {("run_id", "phase", "ordinal"), ("agent_run_id",)},
        "evaluation_results": {("run_id",)},
        "evaluation_events": {("run_id", "sequence")},
    }
    for name, required in identities.items():
        table = Base.metadata.tables[name]
        actual = {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        assert required <= actual
