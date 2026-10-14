"""Holding loop stays bounded/cancellable and never delegates old guard recovery."""

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoveryOwnerState,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "fault", [None, "expired", "revoked", "core", "lost_commit", "cancel", "cancel_lost_commit"]
)
def test_holding_controller_loop_keeps_lease_and_refuses_expiry(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_controller as worker
    from coire_node import expired_recovery_owner as ownership
    from coire_node import expired_recovery_publication as publication
    from coire_node.recovery_lease import bind_recovery_lease

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(bootstrap.root / "expired-recovery")
    PrivateUpgradeStore(runs.root / owner.consumption.transfer.intent.recovery_id)
    settings = Settings(
        node_name="coire-core" if fault == "core" else "coire-edge-a",
        upgrades_enabled=True,
        upgrade_bootstrap_root=str(bootstrap.root),
    )
    held = []

    @contextmanager
    def lease(*args):
        held.append(True)
        try:
            with (
                bootstrap.ownership("recovery-transfer.lock") as descriptor,
                bind_recovery_lease(bootstrap, owner.sha256(), descriptor),
            ):
                yield owner
        finally:
            held.pop()

    initial = Mock(side_effect=lease)
    resume = Mock(side_effect=lease)
    monkeypatch.setattr(ownership, "activated_recovery_owner", initial)
    monkeypatch.setattr(ownership, "resumed_recovery_owner", resume)
    state = UpgradeExpiredRecoveryOwnerState(
        owner_sha256=owner.sha256(),
        owner=owner if fault in {"lost_commit", "cancel_lost_commit"} else None,
        checked_at=owner.claimed_at,
    )
    ticks = []

    def probe(*args):
        if fault in {"cancel", "cancel_lost_commit"}:
            ticks.append(True)
        return state

    control = SimpleNamespace(probe_recovery_owner=Mock(side_effect=probe))

    def holding(*args):
        assert held
        ticks.append(True)
        if fault == "revoked":
            raise UpgradeUnavailable("fixture revoked")

    monkeypatch.setattr(worker, "publish_holding_controller", holding)
    publish = Mock(side_effect=lambda *args: held and owner)
    monkeypatch.setattr(publication, "publish_unready_support", publish)
    now = (
        owner.consumption.transfer.intent.deadline_at
        if fault == "expired"
        else owner.claimed_at + timedelta(seconds=1)
    )
    monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda zone: now))
    stopped = SimpleNamespace(is_set=lambda: bool(ticks), wait=lambda timeout: timeout <= 1)
    if fault in {"expired", "revoked", "core"}:
        with pytest.raises(UpgradeUnavailable):
            worker.run_holding_controller(settings, owner, control, stopped)
    else:
        worker.run_holding_controller(settings, owner, control, stopped)
        assert len(ticks) == 1
    assert not held
    assert resume.call_count == (1 if fault == "lost_commit" else 0)
    assert initial.call_count == (1 if fault in {None, "revoked"} else 0)
    assert publish.call_count == (
        0 if fault in {"expired", "core", "cancel", "cancel_lost_commit"} else 1
    )
