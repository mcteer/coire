"""Passive same-boot succession facts do not create a replacement claim."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "alive", "boot", "source", "core", "late_source", "slow"])
def test_succession_observer_preserves_owner_and_requires_positive_facts(
    tmp_path, monkeypatch, fault
):
    from coire_node import expired_recovery_succession as worker

    previous = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(root.root / "expired-recovery")
    run = PrivateUpgradeStore(runs.root / previous.consumption.transfer.intent.recovery_id)
    run.write("owner.claim.json", previous)
    before = run.path("owner.claim.json").read_bytes()
    successor = previous.process.model_copy(update={"pid": previous.process.pid + 1})
    monkeypatch.setattr(worker, "verify_passive_support_identity", lambda *args: successor)
    monkeypatch.setattr(
        worker,
        "verify_retired_identity",
        Mock(
            side_effect=UpgradeUnavailable("fixture predecessor present")
            if fault == "alive"
            else None
        ),
    )
    monkeypatch.setattr(
        worker,
        "recovery_kernel_boot_identity",
        lambda *args: "changed" if fault == "boot" else previous.kernel_boot_identity,
    )
    monkeypatch.setattr(worker, "fixed_launchctl_probe", lambda *args: lambda target: None)
    late = [False]
    source = previous.legacy_stop.preserved_source
    monkeypatch.setattr(
        worker,
        "source_identity",
        lambda *args: (
            source.model_copy(
                update={
                    "process": source.process.model_copy(update={"pid": source.process.pid + 1})
                }
            )
            if fault == "source" or (fault == "late_source" and late[0])
            else source
        ),
    )
    control = SimpleNamespace(
        verify_recovery_owner=Mock(
            return_value=previous,
            side_effect=UpgradeUnavailable("fixture revoked") if fault == "core" else None,
        )
    )
    if fault == "late_source":

        def final_gate(*args):
            if control.verify_recovery_owner.call_count == 4:
                late[0] = True
            return previous

        control.verify_recovery_owner.side_effect = final_gate
    if fault == "slow":
        monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=Mock(side_effect=[0.0, 6.0])))
    settings = Settings(upgrade_bootstrap_root=str(root.root))
    if fault:
        with pytest.raises(UpgradeUnavailable):
            worker.observe_same_boot_succession(
                settings, "01ARZ3NDEKTSV4RRFFQ69G5FAX", previous, control
            )
    else:
        proof = worker.observe_same_boot_succession(
            settings, "01ARZ3NDEKTSV4RRFFQ69G5FAX", previous, control
        )
        assert proof.previous_owner == previous and proof.successor == successor
    assert run.path("owner.claim.json").read_bytes() == before
    assert not run.path("successions").exists()
