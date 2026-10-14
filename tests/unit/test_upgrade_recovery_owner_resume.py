"""Committed owner can resume same identity after transfer expiry, not renew it."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "revoked", "kernel", "source"])
def test_resume_requires_fresh_owner_gate_and_unchanged_physical_scope(
    tmp_path, monkeypatch, fault
):
    from coire_node import expired_recovery_owner as worker

    claim = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    receipt = claim.consumption
    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(bootstrap.root / "expired-recovery")
    run = PrivateUpgradeStore(runs.root / receipt.transfer.intent.recovery_id)
    run.write("consumption.json", receipt)
    run.write("legacy-stop.intent.json", claim.legacy_stop.effect)
    run.write("legacy-stop.receipt.json", claim.legacy_stop)
    run.write("owner.claim.json", claim)
    before = (run.root / "owner.claim.json").read_bytes()
    settings = Settings(upgrade_bootstrap_root=str(bootstrap.root))
    monkeypatch.setattr(
        worker,
        "datetime",
        SimpleNamespace(now=lambda zone: receipt.transfer.expires_at + timedelta(seconds=1)),
    )
    monkeypatch.setattr(worker, "verify_local_active_owner_identity", Mock())
    monkeypatch.setattr(worker, "verify_retired_identity", Mock())
    monkeypatch.setattr(worker, "fixed_launchctl_probe", lambda *args: lambda target: None)
    monkeypatch.setattr(
        worker,
        "recovery_kernel_boot_identity",
        lambda *args: "other-kernel" if fault == "kernel" else claim.kernel_boot_identity,
    )
    monkeypatch.setattr(
        worker,
        "source_identity",
        lambda *args: (
            claim.legacy_stop.preserved_source.model_copy(
                update={"recorded_at": receipt.transfer.expires_at}
            )
            if fault == "source"
            else claim.legacy_stop.preserved_source
        ),
    )
    gate = Mock(
        side_effect=UpgradeUnavailable("fixture revoked") if fault == "revoked" else None,
        return_value=claim,
    )
    control = SimpleNamespace(verify_recovery_owner=gate)
    if fault:
        with (
            pytest.raises((UpgradeUnavailable, UpgradeConflict)),
            worker.resumed_recovery_owner(settings, receipt, claim.runtime, control),
        ):
            pytest.fail("uncertain scope resumed")
    else:
        with worker.resumed_recovery_owner(settings, receipt, claim.runtime, control) as active:
            assert active == claim
            with pytest.raises(UpgradeConflict), bootstrap.ownership("recovery-transfer.lock"):
                pytest.fail("second owner acquired")
        assert (run.root / "owner.activation.json").exists()
    assert (run.root / "owner.claim.json").read_bytes() == before
    assert (run.root / "owner.activation.json").exists() == (fault is None)
    gate.assert_called()
