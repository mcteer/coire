"""Session loop cannot manufacture ownership from a retained DTO."""

import threading
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


def test_held_loop_refuses_absent_lifetime_capability_before_publication(tmp_path, monkeypatch):
    from coire_node import expired_recovery_controller as worker

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    publish = Mock()
    monkeypatch.setattr("coire_node.expired_recovery_publication.publish_unready_support", publish)
    with pytest.raises(UpgradeUnavailable):
        worker.run_held_controller(
            Settings(
                upgrades_enabled=True,
                node_name="coire-edge-a",
                upgrade_bootstrap_root=str(root.root),
            ),
            owner,
            Mock(),
            threading.Event(),
        )
    publish.assert_not_called()


def test_held_loop_monotonic_expiry_after_publication_keeps_lease(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    from coire_node import expired_recovery_controller as worker
    from coire_node.recovery_lease import bind_recovery_lease, require_recovery_lease

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    tick = [0.0]
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: tick[0]), raising=False)
    monkeypatch.setattr(
        worker,
        "datetime",
        SimpleNamespace(now=lambda zone: owner.claimed_at + timedelta(seconds=1)),
    )
    stopped = threading.Event()
    monkeypatch.setattr(stopped, "wait", lambda delay: stopped.set())
    heartbeat = Mock()
    monkeypatch.setattr(worker, "publish_holding_controller", heartbeat)

    def publication(*args):
        require_recovery_lease(root, owner.sha256())
        tick[0] = 1000.0

    monkeypatch.setattr(
        "coire_node.expired_recovery_publication.publish_unready_support", publication
    )
    with (
        root.ownership("recovery-transfer.lock") as fd,
        bind_recovery_lease(root, owner.sha256(), fd),
    ):
        with pytest.raises(UpgradeUnavailable):
            worker.run_held_controller(
                Settings(
                    upgrades_enabled=True,
                    node_name="coire-edge-a",
                    upgrade_bootstrap_root=str(root.root),
                ),
                owner,
                Mock(),
                stopped,
            )
        require_recovery_lease(root, owner.sha256())
    heartbeat.assert_not_called()
