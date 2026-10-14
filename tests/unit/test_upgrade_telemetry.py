"""Durable upgrade health remains observable with diagnostic trace export disabled."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from upgrade_fixtures import core_plan

from coire_api.upgrades import telemetry
from coire_core.models.upgrades import UpgradePhase, UpgradeReceipt
from coire_core.settings import Settings


@pytest.mark.parametrize("diagnostics", [False, True])
@pytest.mark.parametrize("fault", [None, "absent", "stale", "future", "foreign", "unready"])
async def test_durable_health_rejects_missing_or_foreign_executor(
    monkeypatch: pytest.MonkeyPatch, diagnostics: bool, fault: str | None
) -> None:
    settings = Settings(upgrades_enabled=True, diagnostics_enabled=diagnostics)
    now = datetime.now(UTC)
    plan = core_plan()
    operation = plan.operation.model_copy(
        update={"phase": UpgradePhase.RECOVERY_REQUIRED, "updated_at": now - timedelta(seconds=301)}
    )
    session = AsyncMock()
    session.scalars.side_effect = [
        SimpleNamespace(all=lambda: [SimpleNamespace(payload=operation.model_dump(mode="json"))]),
        SimpleNamespace(all=lambda: []),
    ]
    session.get.return_value = (
        None
        if fault == "absent"
        else SimpleNamespace(
            identity="foreign" if fault == "foreign" else settings.upgrade_executor_id,
            bootstrap_ready=fault != "unready",
            heartbeat_at=now
            + timedelta(seconds=1 if fault == "future" else -11 if fault == "stale" else 0),
        )
    )
    recorders = {
        name: Mock()
        for name in (
            "enabled",
            "active",
            "phase_age",
            "recovery_required",
            "executor_ready",
            "executor_heartbeat_age",
            "snapshot_timestamp",
        )
    }
    for name, recorder in recorders.items():
        monkeypatch.setattr(telemetry, name, recorder)
    await telemetry.collect_health(session, settings)
    recorders["executor_ready"].set.assert_called_once_with(int(fault is None))
    recorders["active"].set.assert_any_call(1, {"kind": "control_plane"})
    recorders["active"].set.assert_any_call(0, {"kind": "studio"})
    recorders["recovery_required"].set.assert_any_call(1, {"kind": "control_plane"})
    assert recorders["phase_age"].set.call_args_list[1].args[0] >= 301
    recorders["snapshot_timestamp"].set.assert_called_once()
    session.flush.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.add.assert_not_called()


async def test_failed_collection_does_not_publish_a_fresh_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    session.scalars.side_effect = RuntimeError("database unavailable")
    timestamp = Mock()
    monkeypatch.setattr(telemetry, "snapshot_timestamp", timestamp)
    with pytest.raises(RuntimeError):
        await telemetry.collect_health(session, Settings(upgrades_enabled=True))
    timestamp.set.assert_not_called()


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("outcome", ["complete", "failed", "unknown"])
def test_terminal_metrics_distinguish_failed_observations_and_replayed_receipts(
    monkeypatch: pytest.MonkeyPatch,
    replay: bool,
    outcome: str,
) -> None:
    value = core_plan().operation
    finished = value.accepted_at + timedelta(seconds=123)
    operation = value.model_copy(update={"phase": UpgradePhase.VERIFYING_SOURCE})
    receipt = UpgradeReceipt.model_validate(
        {
            "job_id": value.job_id,
            "generation": value.generation,
            "sequence": 1,
            "effect_id": "source_verified",
            "phase": UpgradePhase.ROLLED_BACK if outcome == "complete" else operation.phase,
            "occurred_at": finished,
            "outcome": outcome,
        }
    )
    failures, duration, phases, attempts = Mock(), Mock(), Mock(), Mock()
    for name, recorder in [
        ("failure_observations", failures),
        ("operation_duration", duration),
        ("phase_observations", phases),
        ("receipt_attempts", attempts),
    ]:
        monkeypatch.setattr(telemetry, name, recorder)
    telemetry.observed(operation, receipt, replay=replay)
    attempts.add.assert_called_once()
    if replay:
        failures.add.assert_not_called()
        duration.record.assert_not_called()
        phases.add.assert_not_called()
    else:
        phases.add.assert_called_once()
        if outcome == "complete":
            duration.record.assert_called_once_with(
                123, {"kind": "control_plane", "phase": "rolled_back"}
            )
        else:
            duration.record.assert_not_called()
        if outcome == "failed":
            failures.add.assert_called_once()
        else:
            failures.add.assert_not_called()
