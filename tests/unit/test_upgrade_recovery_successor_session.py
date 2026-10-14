import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionGrant,
    UpgradeExpiredRecoverySuccessionObservation,
    UpgradeExpiredRecoverySuccessorClaim,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "retained", "cancelled", "error"])
def test_initial_session_composes_without_manufacturing_authority(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_successor_session as worker

    previous = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    intent = previous.consumption.transfer.intent
    now = previous.claimed_at + timedelta(seconds=1)
    identifier = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
    grant = UpgradeExpiredRecoverySuccessionGrant(
        observation=UpgradeExpiredRecoverySuccessionObservation(
            succession_id=identifier,
            previous_owner=previous,
            successor=previous.process.model_copy(update={"pid": previous.process.pid + 1}),
            kernel_boot_identity=previous.kernel_boot_identity,
            preserved_source=previous.legacy_stop.preserved_source,
            observed_at=now,
            previous_owner_retired=True,
        ),
        user_id=intent.accepted_by,
        key_id=None,
        credential_version=None,
        accepted_at=now,
        expires_at=now + timedelta(seconds=20),
    )
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(root.path("expired-recovery"))
    run = PrivateUpgradeStore(runs.path(intent.recovery_id))
    run.write("owner.claim.json", previous)
    original = run.path("owner.claim.json").read_bytes()
    expected = UpgradeExpiredRecoverySuccessorClaim(
        grant=grant,
        process=grant.observation.successor,
        runtime=previous.runtime,
        kernel_boot_identity=previous.kernel_boot_identity,
        preserved_source=grant.observation.preserved_source,
        claimed_at=now,
    )
    if fault == "retained":
        run.write(f"successor.{identifier}.claim.json", expected)
    monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda zone: now))
    wait = Mock(
        return_value=grant,
        side_effect=UpgradeUnavailable("fixture transport") if fault == "error" else None,
    )
    holding = Mock()
    monkeypatch.setattr(worker, "wait_for_succession_grant", wait)
    monkeypatch.setattr(worker, "run_successor_holding", holding)
    stopped = threading.Event()
    if fault == "cancelled":
        stopped.set()
    settings = Settings(
        upgrades_enabled=True,
        node_name=previous.consumption.transfer.legacy_guard.node,
        upgrade_bootstrap_root=str(root.root),
    )
    control = object()
    if fault in {"retained", "error"}:
        with pytest.raises(UpgradeUnavailable):
            worker.run_initial_successor_session(settings, previous, identifier, control, stopped)
    else:
        worker.run_initial_successor_session(settings, previous, identifier, control, stopped)
    if fault is None:
        holding.assert_called_once_with(settings, expected, control, stopped)
    else:
        holding.assert_not_called()
    assert wait.call_count == (0 if fault in {"retained", "cancelled"} else 1)
    assert run.path("owner.claim.json").read_bytes() == original
    if fault != "retained":
        assert not run.path(f"successor.{identifier}.claim.json").exists()
