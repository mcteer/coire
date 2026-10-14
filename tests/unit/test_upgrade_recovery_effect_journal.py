"""Durable stop intent never permits an interrupted stop to be blindly repeated."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_upgrade_expired_recovery_transfer import transfer_payload

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryLegacyService,
    UpgradeExpiredRecoveryLegacyStopIntent,
    UpgradeExpiredRecoveryOwnerTransfer,
    UpgradeExpiredRecoveryTransferConsumption,
)
from coire_core.upgrade_journal import PrivateUpgradeStore


def test_stop_journal_binds_managed_service_and_definition(tmp_path):
    from coire_node.expired_recovery_effect_journal import begin_legacy_stop

    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    receipt = UpgradeExpiredRecoveryTransferConsumption(
        consumption_id=uuid4(),
        transfer=transfer,
        consumer=transfer.successor,
        consumed_at=transfer.issued_at,
    )
    run = PrivateUpgradeStore(tmp_path / "run")
    run.write("consumption.json", receipt)
    now = datetime.now(UTC)
    service = UpgradeExpiredRecoveryLegacyService(
        target="system/com.coire.upgrade-guard",
        owner_uid=501,
        abandon_process_group=True,
        process=transfer.legacy_guard.process,
        observation_sha256="a" * 64,
        observed_at=now,
    )
    from coire_core.models.upgrades import NativeNodeProcess

    source = NativeNodeProcess(
        selected=transfer.legacy_guard.selected,
        process=transfer.legacy_guard.process.model_copy(
            update={"pid": transfer.legacy_guard.process.pid + 1000}
        ),
        recorded_at=now,
    )
    intent, fresh = begin_legacy_stop(
        run, receipt, now, service=service, definition_sha256="b" * 64, preserved_source=source
    )
    assert fresh and intent.service == service and intent.definition_sha256 == "b" * 64
    with pytest.raises(UpgradeConflict):
        begin_legacy_stop(
            run, receipt, now, service=service, definition_sha256="c" * 64, preserved_source=source
        )
    assert run.read("legacy-stop.intent.json", UpgradeExpiredRecoveryLegacyStopIntent) == intent


@pytest.mark.parametrize("clock_fault", ["before_consumption", "after_expiry"])
def test_retained_stop_intent_replay_validates_historical_epoch(tmp_path, clock_fault):
    from coire_node.expired_recovery_effect_journal import begin_legacy_stop

    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    receipt = UpgradeExpiredRecoveryTransferConsumption(
        consumption_id=uuid4(),
        transfer=transfer,
        consumer=transfer.successor,
        consumed_at=transfer.issued_at,
    )
    run = PrivateUpgradeStore(tmp_path / "run")
    run.write("consumption.json", receipt)
    intent, _ = begin_legacy_stop(run, receipt, datetime.now(UTC))
    bad = intent.model_copy(
        update={
            "started_at": receipt.consumed_at - timedelta(seconds=1)
            if clock_fault == "before_consumption"
            else transfer.expires_at
        }
    )
    run.write("legacy-stop.intent.json", bad)  # Synthetic corruption, never live authority.
    with pytest.raises(UpgradeConflict):
        begin_legacy_stop(run, receipt, datetime.now(UTC))
    assert run.read("legacy-stop.intent.json", UpgradeExpiredRecoveryLegacyStopIntent) == bad


def test_stop_intent_lost_response_retains_single_effect(tmp_path, monkeypatch):
    from coire_node.expired_recovery_effect_journal import begin_legacy_stop

    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    receipt = UpgradeExpiredRecoveryTransferConsumption(
        consumption_id=uuid4(),
        transfer=transfer,
        consumer=transfer.successor,
        consumed_at=transfer.issued_at,
    )
    run = PrivateUpgradeStore(tmp_path / "run")
    run.write("consumption.json", receipt)
    write = run.write

    def interrupted(key, value, *, replace=True):
        write(key, value, replace=replace)
        raise RuntimeError("fixture interrupted publication")

    with monkeypatch.context() as patch:
        patch.setattr(run, "write", interrupted)
        with pytest.raises(RuntimeError, match="interrupted publication"):
            begin_legacy_stop(run, receipt, datetime.now(UTC))
    intent, fresh = begin_legacy_stop(run, receipt, datetime.now(UTC))
    assert not fresh  # Native producer must inspect, never repeat from an intent.
    assert intent.consumption_sha256 == receipt.sha256()
    assert intent.legacy_guard == transfer.legacy_guard
    assert begin_legacy_stop(run, receipt, datetime.now(UTC)) == (intent, False)
