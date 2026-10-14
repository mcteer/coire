"""Historical owner absence/errors never select legacy budget or delegates."""

import threading
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("state", ["initial", "owner", "corrupt"])
def test_support_mode_selects_exact_retained_owner_no_error_fallback(tmp_path, monkeypatch, state):
    from coire_node import expired_recovery_support as worker

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    intent = owner.consumption.transfer.intent
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(root.root / "expired-recovery")
    run = PrivateUpgradeStore(runs.root / intent.recovery_id)
    if state == "owner":
        run.write("owner.claim.json", owner)
    elif state == "corrupt":
        run.path("owner.claim.json").write_text("{}")
        run.path("owner.claim.json").chmod(0o600)
    initial = Mock()
    resumed = Mock()
    monkeypatch.setattr(worker, "run_initial_support_session", initial)
    monkeypatch.setattr("coire_node.expired_recovery_controller.run_holding_controller", resumed)
    control = Mock()
    control.verify_support_context.return_value = intent
    settings = Settings(
        node_name="coire-edge-a", upgrades_enabled=True, upgrade_bootstrap_root=str(root.root)
    )
    if state == "corrupt":
        with pytest.raises(UpgradeUnavailable):
            worker.run_support_mode(settings, intent, owner.runtime, control, threading.Event())
    else:
        worker.run_support_mode(settings, intent, owner.runtime, control, threading.Event())
    assert initial.call_count == (1 if state == "initial" else 0)
    assert resumed.call_count == (1 if state == "owner" else 0)
