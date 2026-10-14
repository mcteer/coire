"""Holding heartbeat never publishes Source readiness or alters original metadata."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "revoked", "no_lease"])
def test_controller_holding_snapshot_requires_live_scoped_owner(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_controller as worker
    from coire_node.recovery_lease import bind_recovery_lease

    claim = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(bootstrap.root / "expired-recovery")
    run = PrivateUpgradeStore(runs.root / claim.consumption.transfer.intent.recovery_id)
    run.write("owner.activation.json", claim)
    settings = Settings(upgrade_bootstrap_root=str(bootstrap.root))
    monkeypatch.setattr(worker, "verify_local_active_owner_identity", Mock())
    monkeypatch.setattr(worker, "verify_retired_identity", Mock())
    monkeypatch.setattr(worker, "fixed_launchctl_probe", lambda *args: lambda target: None)
    monkeypatch.setattr(worker, "source_identity", lambda *args: claim.legacy_stop.preserved_source)
    gate = Mock(
        side_effect=UpgradeUnavailable("fixture revoked") if fault == "revoked" else None,
        return_value=claim,
    )
    control = SimpleNamespace(verify_recovery_owner=gate)
    if fault == "no_lease":
        with pytest.raises(UpgradeUnavailable):
            worker.publish_holding_controller(settings, claim, control)
        gate.assert_not_called()
    else:
        with (
            bootstrap.ownership("recovery-transfer.lock") as fd,
            bind_recovery_lease(bootstrap, claim.sha256(), fd),
        ):
            if fault:
                with pytest.raises(UpgradeUnavailable):
                    worker.publish_holding_controller(settings, claim, control)
            else:
                snapshot = worker.publish_holding_controller(settings, claim, control)
                assert snapshot.owner == claim and snapshot.ready is False
    assert (run.root / "controller.status.json").exists() == (fault is None)
    assert not (bootstrap.root / "status.json").exists()
    assert not (bootstrap.root / "runtime.json").exists()
