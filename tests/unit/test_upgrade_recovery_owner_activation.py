"""Core acknowledgement cannot activate without lease and fresh owner gate."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "revoked", "lost_write", "changed_ack"])
def test_activation_retains_exact_ack_under_lifetime_lease(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_owner as worker

    claim = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(bootstrap.root / "expired-recovery")
    run = PrivateUpgradeStore(runs.root / claim.consumption.transfer.intent.recovery_id)
    settings = Settings(upgrade_bootstrap_root=str(bootstrap.root))
    held = []

    @contextmanager
    def lease(*args):
        held.append(True)
        try:
            from coire_node.recovery_lease import bind_recovery_lease

            with (
                bootstrap.ownership("recovery-transfer.lock") as descriptor,
                bind_recovery_lease(bootstrap, claim.sha256(), descriptor),
            ):
                yield claim
        finally:
            held.pop()

    monkeypatch.setattr(worker, "recovery_owner_lease", lease)

    def accept(value):
        assert held and value == claim
        return (
            claim.model_copy(update={"kernel_boot_identity": "different-kernel"})
            if fault == "changed_ack"
            else claim
        )

    def verify(value):
        assert held and value == claim
        if fault == "revoked":
            raise UpgradeUnavailable("fixture revoked")
        return claim

    control = SimpleNamespace(
        accept_recovery_owner=Mock(side_effect=accept),
        verify_recovery_owner=Mock(side_effect=verify),
    )
    if fault == "lost_write":
        write = PrivateUpgradeStore.write

        def lost(self, key, value, *, replace=True):
            write(self, key, value, replace=replace)
            if key == "owner.activation.json":
                raise RuntimeError("fixture lost write response")

        monkeypatch.setattr(PrivateUpgradeStore, "write", lost)
    if fault:
        with (
            pytest.raises(RuntimeError if fault == "lost_write" else UpgradeUnavailable),
            worker.activated_recovery_owner(settings, claim.consumption, claim.runtime, control),
        ):
            pytest.fail("failed gate activated")
    else:
        with worker.activated_recovery_owner(
            settings, claim.consumption, claim.runtime, control
        ) as active:
            assert held and active == claim
        assert control.accept_recovery_owner.call_count == 1
    assert not held
    assert (run.root / "owner.activation.json").exists() == (
        fault not in {"revoked", "changed_ack"}
    )
