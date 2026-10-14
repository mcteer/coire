"""A step failure or shutdown never turns retained intent into released ownership."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import Mock

import pytest
from test_executor_journal import activation
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import UpgradePhase, UpgradeReceipt, UpgradeReceiptAck
from coire_core.settings import Settings
from coire_upgrade_executor.executor import CoreExecutor
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.worker import observe_deadlines, run_owned_operation


@pytest.mark.parametrize("shutdown", [False, True])
async def test_owned_loop_retries_without_releasing_uncertain_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    shutdown: bool,
) -> None:
    plan = core_plan()
    journal = ExecutorJournal(tmp_path / "journal", plan)
    receipt = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        sequence=1,
        phase=UpgradePhase.SUCCEEDED,
        effect_id="terminal",
        occurred_at=datetime.now(UTC),
        outcome="complete",
    )
    journal.receipts.append(receipt)
    # Compare all receipt files instead of relying on the journal's filename ABI.
    before = {path.name: path.read_bytes() for path in journal.receipts.store.root.iterdir()}
    authority = SimpleNamespace(verify=Mock(), claim=SimpleNamespace(state="active"))
    executor = cast(CoreExecutor, SimpleNamespace(journal=journal, authority=authority))
    stop = asyncio.Event()
    calls = 0

    async def step(_executor: CoreExecutor) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise UpgradeUnavailable("private-engine-output-must-not-be-logged")
        if shutdown:
            stop.set()
            return
        journal.receipts.acknowledge(
            UpgradeReceiptAck(
                job_id=receipt.job_id,
                generation=receipt.generation,
                sequence=receipt.sequence,
            )
        )
        authority.claim.state = "committed"

    monkeypatch.setattr("coire_upgrade_executor.worker.advance_core_step", step)
    async with asyncio.timeout(2):
        await run_owned_operation(executor, Settings(upgrade_poll_s=0.001), stop)
    assert calls == 2
    assert "private-engine-output" not in caplog.text
    assert authority.claim.state == ("active" if shutdown else "committed")
    assert journal.receipts.acknowledged_sequence() == (0 if shutdown else 1)
    assert all(
        journal.receipts.store.path(name).read_bytes() == content
        for name, content in before.items()
    )


async def test_changed_kernel_owner_terminates_loop_before_any_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = ExecutorJournal(tmp_path / "journal", core_plan())
    authority = SimpleNamespace(verify=Mock(side_effect=UpgradeConflict("changed owner")))
    executor = cast(CoreExecutor, SimpleNamespace(journal=journal, authority=authority))
    step = Mock()
    monkeypatch.setattr("coire_upgrade_executor.worker.advance_core_step", step)
    with pytest.raises(UpgradeConflict):
        await run_owned_operation(executor, Settings(), asyncio.Event())
    step.assert_not_called()


@pytest.mark.parametrize(
    "phase, outcome, recovering, overdue",
    [
        (UpgradePhase.VERIFYING, "intent", False, 280),
        (UpgradePhase.VERIFYING, "complete", False, 0),
        (UpgradePhase.COMMITTING, "intent", False, 0),
        (UpgradePhase.RESTORING, "intent", True, 99),
        (UpgradePhase.VERIFYING_SOURCE, "complete", True, 0),
    ],
)
def test_deadline_metrics_keep_original_budget_and_clear_verified_obligations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: UpgradePhase,
    outcome: Literal["intent", "complete"],
    recovering: bool,
    overdue: int,
) -> None:
    import coire_upgrade_executor.worker as worker

    journal = ExecutorJournal(tmp_path / "journal", core_plan())
    now = datetime.now(UTC)
    journal.activation(activation(journal, now), now=now)
    if recovering:
        journal.capture_recovery(now=now + timedelta(seconds=1))
    journal.receipts.append(
        UpgradeReceipt(
            job_id=journal.plan.operation.job_id,
            generation=journal.plan.operation.generation,
            sequence=1,
            phase=phase,
            effect_id="progress",
            occurred_at=now,
            outcome=outcome,
        )
    )
    state_before = journal.store.path("epoch.json").read_bytes()
    clock = Mock(wraps=datetime)
    clock.now.return_value = now + timedelta(seconds=400)
    monkeypatch.setattr(worker, "datetime", clock)
    gauge = Mock()
    monkeypatch.setattr(worker, "deadline_overdue", gauge)
    observe_deadlines(cast(CoreExecutor, SimpleNamespace(journal=journal)))
    observed = {call.args[1]["boundary"]: call.args[0] for call in gauge.set.call_args_list}
    assert observed["recovery" if recovering else "runtime"] == overdue
    assert observed["outage"] == 0
    if recovering:
        assert observed["runtime"] == 0
    assert journal.store.path("epoch.json").read_bytes() == state_before


def test_service_deadline_alerts_before_global_budget_without_capturing_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_executor_cohort import cohort_plan

    import coire_upgrade_executor.worker as worker

    journal = ExecutorJournal(tmp_path / "journal", cohort_plan())
    now = datetime.now(UTC)
    state = journal.activation(activation(journal, now), now=now)
    window = state.service_windows[0]
    detected = window.deadline_at + timedelta(seconds=7)
    assert state.readiness_deadline is not None and detected < state.readiness_deadline
    before = journal.store.path("epoch.json").read_bytes()
    clock = Mock(wraps=datetime)
    clock.now.return_value = detected
    monkeypatch.setattr(worker, "datetime", clock)
    gauge = Mock()
    monkeypatch.setattr(worker, "deadline_overdue", gauge)
    observe_deadlines(cast(CoreExecutor, SimpleNamespace(journal=journal)))
    observed = {call.args[1]["boundary"]: call.args[0] for call in gauge.set.call_args_list}
    assert observed == {"runtime": 0, "outage": 0, "recovery": 0, "forward": 7}
    assert journal.store.path("epoch.json").read_bytes() == before
