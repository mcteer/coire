"""Passive process publication follows authenticated scope, never legacy effects."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_expired_recovery_transfer import transfer_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerTransfer
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "scope", "identity"])
def test_passive_support_capture_has_no_original_side_effects(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_support as worker

    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    intent = transfer.intent
    support = intent.support_release.manifest
    runtime = transfer.legacy_guard.bootstrap_runtime.model_copy(
        update={
            "environment_id": support.environment_id,
            "environment_sha256": support.environment_sha256,
        }
    )
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    settings = Settings(upgrade_bootstrap_root=str(root.root))
    control = SimpleNamespace(
        verify_support_context=Mock(
            side_effect=UpgradeUnavailable("fixture scope denied") if fault == "scope" else None,
            return_value=intent,
        )
    )
    identity = Mock(
        side_effect=UpgradeUnavailable("fixture identity denied") if fault == "identity" else None,
        return_value=transfer.successor,
    )
    monkeypatch.setattr(worker, "verify_passive_support_identity", identity)
    if fault:
        with pytest.raises(UpgradeUnavailable):
            worker.capture_support_process(settings, intent, runtime, control)
        assert not (root.root / "expired-recovery").exists()
    else:
        first = worker.capture_support_process(settings, intent, runtime, control)
        assert first.process == transfer.successor
        assert worker.capture_support_process(settings, intent, runtime, control) == first
    assert not (root.root / "runtime.json").exists()
    assert not (root.root / "status.json").exists()
    assert not (root.root / "claim.json").exists()
