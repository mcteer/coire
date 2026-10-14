"""Uncertain-stop reconciliation observes facts, never retries bootout."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "loaded",
        "alive",
        "source",
        "definition",
        "revoked",
        "lost_response",
        "cancel_final_probe",
        "elapsed_final_probe",
        "elapsed_initial_probe",
        "elapsed_sampling_probe",
    ],
)
def test_observed_stop_reconciliation_no_command_retry(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_stop_reconciliation as worker

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    receipt = owner.consumption
    effect = owner.legacy_stop.effect
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(root.root / "expired-recovery")
    run = PrivateUpgradeStore(runs.root / receipt.transfer.intent.recovery_id)
    run.write("consumption.json", receipt)
    run.write("legacy-stop.intent.json", effect)
    monkeypatch.setattr(worker, "verify_local_successor_identity", lambda *args: None)
    monkeypatch.setattr(
        worker,
        "fixed_launchctl_probe",
        lambda *args: lambda target: object() if fault == "loaded" else None,
    )
    monkeypatch.setattr(
        worker,
        "verify_retired_identity",
        Mock(side_effect=UpgradeUnavailable("fixture still alive") if fault == "alive" else None),
    )
    monkeypatch.setattr(
        worker,
        "source_identity",
        lambda *args: (
            effect.preserved_source.model_copy(
                update={
                    "process": effect.preserved_source.process.model_copy(
                        update={"pid": effect.preserved_source.process.pid + 1}
                    )
                }
            )
            if fault == "source"
            else effect.preserved_source
        ),
    )
    monkeypatch.setattr(
        worker,
        "read_legacy_definition",
        lambda *args: "f" * 64 if fault == "definition" else effect.definition_sha256,
    )
    command = Mock()
    monkeypatch.setattr("coire_node.expired_recovery_stop.capture_owned_command", command)
    control = SimpleNamespace(
        verify_recovery_consumption=Mock(
            return_value=receipt,
            side_effect=[receipt, receipt, UpgradeUnavailable("fixture revoked")]
            if fault == "revoked"
            else None,
        )
    )
    events = Mock()
    monkeypatch.setattr(worker, "reconciliation_events", events)
    stopped = threading.Event()
    tick = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: tick[0])
    if fault in {
        "cancel_final_probe",
        "elapsed_final_probe",
        "elapsed_initial_probe",
        "elapsed_sampling_probe",
    }:
        probes = [0]

        def verify(current):
            probes[0] += 1
            expiry_probe = {"elapsed_initial_probe": 1, "elapsed_sampling_probe": 2}.get(fault, 4)
            if probes[0] == expiry_probe:
                if fault == "cancel_final_probe":
                    stopped.set()
                else:
                    tick[0] = 31.0
            return current

        control.verify_recovery_consumption.side_effect = verify
    settings = Settings(upgrade_bootstrap_root=str(root.root))
    if fault == "lost_response":
        write = PrivateUpgradeStore.write

        def interrupted(self, key, value, *, replace=True):
            write(self, key, value, replace=replace)
            if key == "legacy-stop.reconciliation.json":
                raise RuntimeError("fixture lost reconciliation response")

        with monkeypatch.context() as patch:
            patch.setattr(PrivateUpgradeStore, "write", interrupted)
            with pytest.raises(RuntimeError, match="lost reconciliation response"):
                worker.reconcile_legacy_stop(settings, receipt, owner.runtime, control)
        from coire_core.models.upgrade_recovery import (
            UpgradeExpiredRecoveryLegacyStopReconciliation,
        )

        retained = run.read(
            "legacy-stop.reconciliation.json", UpgradeExpiredRecoveryLegacyStopReconciliation
        )
        assert worker.reconcile_legacy_stop(settings, receipt, owner.runtime, control) == retained
    elif fault:
        with pytest.raises(UpgradeUnavailable):
            worker.reconcile_legacy_stop(
                settings,
                receipt,
                owner.runtime,
                control,
                **({"stopped": stopped} if fault == "cancel_final_probe" else {}),
            )
        assert not run.path("legacy-stop.reconciliation.json").exists()
        events.add.assert_called_once_with(1, {"outcome": "refused"})
    else:
        first = worker.reconcile_legacy_stop(settings, receipt, owner.runtime, control)
        assert worker.reconcile_legacy_stop(settings, receipt, owner.runtime, control) == first
    command.assert_not_called()
    assert not run.path("legacy-stop.receipt.json").exists()
    assert run.read("legacy-stop.intent.json", type(effect)) == effect
