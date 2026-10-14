"""Observed retirement adoption still needs actual private lifetime exclusion."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryLegacyStopReconciliation,
    UpgradeExpiredRecoveryOwnerClaim,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


def test_reconciled_initial_lease_and_lost_claim_response(tmp_path, monkeypatch):
    from coire_node import expired_recovery_owner as worker

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    receipt = owner.consumption
    stop = UpgradeExpiredRecoveryLegacyStopReconciliation(
        consumption=receipt,
        effect=owner.legacy_stop.effect,
        preserved_source=owner.legacy_stop.preserved_source,
        observed_at=owner.legacy_stop.stopped_at,
        service_unloaded=True,
        legacy_identity_retired=True,
    )
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(root.root / "expired-recovery")
    run = PrivateUpgradeStore(runs.root / receipt.transfer.intent.recovery_id)
    run.write("consumption.json", receipt)
    run.write("legacy-stop.intent.json", stop.effect)
    run.write("legacy-stop.reconciliation.json", stop)
    monkeypatch.setattr(worker, "verify_local_successor_identity", lambda *args: None)
    monkeypatch.setattr(worker, "verify_retired_identity", lambda *args: None)
    monkeypatch.setattr(worker, "fixed_launchctl_probe", lambda *args: lambda target: None)
    monkeypatch.setattr(worker, "source_identity", lambda *args: stop.preserved_source)
    monkeypatch.setattr(
        worker, "recovery_kernel_boot_identity", lambda *args: owner.kernel_boot_identity
    )
    control = SimpleNamespace(verify_recovery_consumption=Mock(return_value=receipt))
    settings = Settings(upgrade_bootstrap_root=str(root.root))
    write = PrivateUpgradeStore.write

    def interrupted(self, key, value, *, replace=True):
        write(self, key, value, replace=replace)
        if key == "owner.claim.json":
            raise RuntimeError("fixture lost claim response")

    with monkeypatch.context() as patch:
        patch.setattr(PrivateUpgradeStore, "write", interrupted)
        with (
            pytest.raises(RuntimeError, match="lost claim response"),
            worker.recovery_owner_lease(settings, receipt, owner.runtime, control),
        ):
            pytest.fail("lost response entered owner context")
    with worker.recovery_owner_lease(settings, receipt, owner.runtime, control) as adopted:
        assert adopted.legacy_stop == stop
        with pytest.raises(UpgradeConflict), root.ownership("recovery-transfer.lock"):
            pytest.fail("concurrent owner took lifetime lock")
    with root.ownership("recovery-transfer.lock"):
        assert run.read("owner.claim.json", UpgradeExpiredRecoveryOwnerClaim) == adopted
    assert not run.path("legacy-stop.receipt.json").exists()
