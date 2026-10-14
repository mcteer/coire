"""Durable intent selects observation, not exception-to-command retry."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("state", ["fresh", "uncertain", "retained", "corrupt"])
def test_initial_retirement_selects_durable_evidence(tmp_path, monkeypatch, state):
    from coire_node import expired_recovery_stop_reconciliation as worker

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(root.root / "expired-recovery")
    run = PrivateUpgradeStore(runs.root / owner.consumption.transfer.intent.recovery_id)
    run.write("consumption.json", owner.consumption)
    if state != "fresh":
        run.write("legacy-stop.intent.json", owner.legacy_stop.effect)
    if state == "retained":
        run.write("legacy-stop.receipt.json", owner.legacy_stop)
    if state == "corrupt":
        run.path("legacy-stop.intent.json").write_text("{}")
    command = Mock(return_value=owner.legacy_stop)
    observation = Mock(return_value=owner.legacy_stop)
    monkeypatch.setattr("coire_node.expired_recovery_stop.stop_legacy_guard", command)
    monkeypatch.setattr(worker, "reconcile_legacy_stop", observation)
    control = SimpleNamespace(verify_recovery_consumption=Mock(return_value=owner.consumption))
    settings = Settings(upgrade_bootstrap_root=str(root.root))
    stopped = threading.Event()
    if state == "corrupt":
        with pytest.raises(UpgradeUnavailable):
            worker.ensure_initial_retirement(
                settings, owner.consumption, owner.runtime, control, stopped=stopped
            )
    else:
        assert (
            worker.ensure_initial_retirement(
                settings, owner.consumption, owner.runtime, control, stopped=stopped
            )
            == owner.legacy_stop
        )
    assert command.call_count == (1 if state == "fresh" else 0)
    if state == "fresh":
        assert command.call_args.kwargs["stopped"] is stopped
    assert observation.call_count == (1 if state == "uncertain" else 0)


@pytest.mark.parametrize("uncertain", [False, True])
def test_support_cancellation_during_retirement_core_probe_stops_dispatch(
    tmp_path, monkeypatch, uncertain
):
    from coire_node import expired_recovery_stop_reconciliation as retirement
    from coire_node import expired_recovery_support as support

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(root.root / "expired-recovery")
    run = PrivateUpgradeStore(runs.root / owner.consumption.transfer.intent.recovery_id)
    run.write("consumption.json", owner.consumption)
    if uncertain:
        run.write("legacy-stop.intent.json", owner.legacy_stop.effect)
    stopped = threading.Event()

    def verify(receipt):
        stopped.set()
        return receipt

    control = SimpleNamespace(verify_recovery_consumption=verify)
    command = Mock(return_value=owner.legacy_stop)
    observation = Mock(return_value=owner.legacy_stop)
    monkeypatch.setattr("coire_node.expired_recovery_stop.stop_legacy_guard", command)
    monkeypatch.setattr(retirement, "reconcile_legacy_stop", observation)
    monkeypatch.setattr(support, "wait_for_consumed_transfer", lambda *args: owner.consumption)
    settings = Settings(upgrade_bootstrap_root=str(root.root))
    try:
        support.run_initial_support_session(
            settings, owner.consumption.transfer.intent, owner.runtime, control, stopped
        )
    except UpgradeUnavailable:
        assert stopped.is_set()
    command.assert_not_called()
    observation.assert_not_called()
    assert not run.path("legacy-stop.receipt.json").exists()
