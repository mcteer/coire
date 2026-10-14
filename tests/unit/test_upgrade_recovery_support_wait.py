"""Passive waiting is cancellable and binds consumed intent/actual captured PID."""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_upgrade_expired_recovery_transfer import transfer_payload

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryConsumptionState,
    UpgradeExpiredRecoveryOwnerTransfer,
    UpgradeExpiredRecoverySupportProcess,
    UpgradeExpiredRecoveryTransferConsumption,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "cancel",
        "foreign",
        "pending",
        "clock_rollback",
        "delivery_cancel",
        "capture_cancel",
        "context_cancel",
    ],
)
def test_passive_wait_never_stops_or_claims_legacy_owner(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_support as worker

    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    manifest = transfer.intent.support_release.manifest
    runtime = transfer.legacy_guard.bootstrap_runtime.model_copy(
        update={
            "environment_id": manifest.environment_id,
            "environment_sha256": manifest.environment_sha256,
        }
    )
    passive = UpgradeExpiredRecoverySupportProcess(
        intent=transfer.intent,
        runtime=runtime,
        process=transfer.successor,
        observed_at=transfer.issued_at,
    )
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(root.root / "expired-recovery")
    run = PrivateUpgradeStore(runs.root / transfer.intent.recovery_id)
    receipt = UpgradeExpiredRecoveryTransferConsumption(
        consumption_id=uuid4(),
        transfer=transfer,
        consumer=transfer.successor,
        consumed_at=transfer.issued_at,
    )
    run.write(
        "consumption.json",
        receipt.model_copy(
            update={
                "consumer": receipt.consumer.model_copy(update={"pid": receipt.consumer.pid + 1}),
                "transfer": transfer.model_copy(
                    update={
                        "successor": receipt.consumer.model_copy(
                            update={"pid": receipt.consumer.pid + 1}
                        )
                    }
                ),
            }
        )
        if fault == "foreign"
        else receipt,
    )
    capture = Mock(return_value=passive)
    monkeypatch.setattr(worker, "capture_support_process", capture)
    from datetime import UTC, datetime

    pending = UpgradeExpiredRecoveryConsumptionState(
        intent_sha256=transfer.intent.sha256(),
        consumption=None
        if fault == "pending"
        else run.read("consumption.json", UpgradeExpiredRecoveryTransferConsumption),
        checked_at=datetime.now(UTC),
    )
    prepare = Mock(return_value=(receipt, False))
    monkeypatch.setattr("coire_node.expired_recovery_prepare.prepare_local_transfer", prepare)
    control = SimpleNamespace(
        probe_recovery_consumption=Mock(return_value=pending),
        verify_support_context=Mock(return_value=transfer.intent),
        verify_recovery_consumption=Mock(return_value=receipt),
    )
    if fault == "clock_rollback":
        from datetime import timedelta

        now = [transfer.issued_at]
        tick = [0.0]
        monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda zone: now[0]))
        monkeypatch.setattr(
            worker, "time", SimpleNamespace(monotonic=lambda: tick[0]), raising=False
        )

        def delivery(*args):
            tick[0] = 1000.0
            now[0] -= timedelta(seconds=60)
            return pending

        control.probe_recovery_consumption.side_effect = delivery
    waits = []
    cancelled = [False]
    stopped = SimpleNamespace(
        is_set=lambda: fault == "cancel" or cancelled[0] or bool(waits),
        wait=lambda timeout: waits.append(timeout),
    )
    if fault in {"capture_cancel", "context_cancel"}:

        def cancel_capture(*args):
            cancelled[0] = True
            return passive

        def cancel_context(*args):
            cancelled[0] = True
            return transfer.intent

        if fault == "capture_cancel":
            capture.side_effect = cancel_capture
        else:
            control.verify_support_context.side_effect = cancel_context
    if fault == "delivery_cancel":

        def cancel_delivery(*args):
            cancelled[0] = True
            return pending

        control.probe_recovery_consumption.side_effect = cancel_delivery
    settings = Settings(upgrade_bootstrap_root=str(root.root))
    if fault == "clock_rollback":
        with pytest.raises(UpgradeUnavailable):
            worker.wait_for_consumed_transfer(settings, transfer.intent, runtime, control, stopped)
        control.verify_recovery_consumption.assert_not_called()
    elif fault == "foreign":
        with pytest.raises(UpgradeConflict):
            worker.wait_for_consumed_transfer(settings, transfer.intent, runtime, control, stopped)
    else:
        result = worker.wait_for_consumed_transfer(
            settings, transfer.intent, runtime, control, stopped
        )
        assert result == (
            None
            if fault in {"cancel", "pending", "delivery_cancel", "capture_cancel", "context_cancel"}
            else receipt
        )
    if fault in {"delivery_cancel", "capture_cancel", "context_cancel"}:
        control.verify_recovery_consumption.assert_not_called()
    if fault in {"capture_cancel", "context_cancel"}:
        control.probe_recovery_consumption.assert_not_called()
    if fault == "capture_cancel":
        control.verify_support_context.assert_not_called()
    if fault in {
        "cancel",
        "pending",
        "foreign",
        "clock_rollback",
        "delivery_cancel",
        "capture_cancel",
        "context_cancel",
    }:
        prepare.assert_not_called()
    else:
        prepare.assert_called_once()
    assert all(0 < timeout <= 1 for timeout in waits)
    if fault == "cancel":
        capture.assert_not_called()
        control.verify_recovery_consumption.assert_not_called()
    assert not (root.root / "runtime.json").exists()
    assert not (run.root / "legacy-stop.intent.json").exists()
