"""The fixed one-shot entrypoint never retries retained or expired migration effects."""

import signal
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from upgrade_fixtures import core_plan

from coire_api.upgrades import migration_runner
from coire_core.errors import UpgradeForbidden
from coire_core.models.upgrades import UpgradeMigrationExecution
from coire_core.settings import Settings


def test_normal_install_uses_fixed_alembic_head(monkeypatch: pytest.MonkeyPatch) -> None:
    upgrade = Mock()
    monkeypatch.setattr(sys, "argv", ["migration_runner"])
    monkeypatch.setattr(migration_runner, "Settings", lambda: Settings())
    monkeypatch.setattr(migration_runner.command, "upgrade", upgrade)
    assert migration_runner.main() == 0
    assert upgrade.call_args.args[0].config_file_name == "/app/alembic.ini"
    assert upgrade.call_args.args[1] == "head"
    monkeypatch.setattr(sys, "argv", ["migration_runner", "another-command"])
    assert migration_runner.main() == 2
    assert upgrade.call_count == 1


@pytest.mark.parametrize("fault", [None, "authority", "expired", "replay", "migration", "record"])
def test_managed_entrypoint_obeys_captured_budget_and_fixed_target(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: str | None,
) -> None:
    plan = core_plan(schema_changed=True)
    now = datetime.now(UTC)
    captured = now - timedelta(seconds=121) if fault == "expired" else now
    intent = UpgradeMigrationExecution(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        plan_sha256=plan.authority_sha256(),
        grant_id=uuid4(),
        state="intent",
        captured_at=captured,
        deadline_at=captured + timedelta(seconds=120),
        schema_before_head=plan.initial_schema_head,
        schema_before_sha256=plan.initial_schema_sha256,
        expected_schema_head=plan.schema_head_after,
    )
    complete = UpgradeMigrationExecution.model_validate(
        intent.model_dump()
        | {
            "state": "complete",
            "exit_code": 0,
            "schema_after_head": plan.schema_head_after,
            "schema_after_sha256": "c" * 64,
            "completed_at": captured + timedelta(seconds=1),
        }
    )
    claim = AsyncMock(return_value=(plan, complete if fault == "replay" else intent))
    if fault == "authority":
        claim.side_effect = UpgradeForbidden()
    record = AsyncMock(return_value=complete)
    if fault == "record":
        record.side_effect = RuntimeError("private-database-credential")
    elif fault == "migration":
        record.return_value = UpgradeMigrationExecution.model_validate(
            intent.model_dump()
            | {
                "state": "uncertain",
                "exit_code": 1,
                "schema_after_head": plan.initial_schema_head,
                "schema_after_sha256": plan.initial_schema_sha256,
                "completed_at": now,
            }
        )
    upgrade = Mock(
        side_effect=RuntimeError("private-database-credential") if fault == "migration" else None
    )
    settings = Settings(
        upgrades_enabled=True,
        upgrade_migration_job_id=plan.operation.job_id,
        upgrade_migration_generation=plan.operation.generation,
    )
    monkeypatch.setattr(sys, "argv", ["migration_runner"])
    monkeypatch.setattr(migration_runner, "Settings", lambda: settings)
    monkeypatch.setattr(migration_runner, "_claim", claim)
    monkeypatch.setattr(migration_runner, "_record", record)
    monkeypatch.setattr(migration_runner.command, "upgrade", upgrade)
    alarm = Mock()
    monkeypatch.setattr(signal, "signal", Mock())
    monkeypatch.setattr(signal, "setitimer", alarm)
    assert migration_runner.main() == (0 if fault in {None, "replay"} else 1)
    if fault in {"authority", "expired", "replay"}:
        upgrade.assert_not_called()
        record.assert_not_awaited()
        alarm.assert_not_called()
    else:
        assert upgrade.call_args.args[1] == plan.schema_head_after
        assert upgrade.call_args.args[0].attributes["coire_upgrade_deadline"] == intent.deadline_at
        assert 0 < alarm.call_args_list[0].args[1] <= 120
        assert alarm.call_args_list[-1].args[1] == 0
        assert record.call_args.args[-1] == (1 if fault == "migration" else 0)
    assert "private-database-credential" not in capsys.readouterr().out
