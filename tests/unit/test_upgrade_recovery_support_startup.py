"""Installed startup context is immutable metadata, not bootstrap activation."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "core", "bytes"])
def test_prepare_support_startup_no_original_metadata_effects(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_support as worker

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    intent = owner.consumption.transfer.intent
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    control = SimpleNamespace(
        verify_support_context=Mock(
            side_effect=UpgradeUnavailable("fixture scope denied") if fault == "core" else None,
            return_value=intent,
        )
    )
    monkeypatch.setattr(
        worker,
        "verify_prepared_runtime",
        Mock(side_effect=UpgradeUnavailable("fixture bytes denied") if fault == "bytes" else None),
    )
    settings = Settings(
        node_name="coire-edge-a", upgrades_enabled=True, upgrade_bootstrap_root=str(root.root)
    )
    if fault:
        with pytest.raises(UpgradeUnavailable):
            worker.prepare_support_startup(settings, intent, owner.runtime, control)
        assert not (root.root / "expired-recovery").exists()
    else:
        first = worker.prepare_support_startup(settings, intent, owner.runtime, control)
        assert worker.prepare_support_startup(settings, intent, owner.runtime, control) == first
    assert not (root.root / "runtime.json").exists()
    assert not (root.root / "status.json").exists()
    assert not (root.root / "claim.json").exists()
