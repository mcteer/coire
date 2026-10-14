"""Write-ahead support publication stays unready and preserves original claim."""

from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.models.upgrades import NativeUpgradeClaim
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("lost", [False, True])
def test_support_unready_publication_reconciles_partial_metadata(tmp_path, monkeypatch, lost):
    from coire_node import expired_recovery_publication as worker
    from coire_node.recovery_lease import bind_recovery_lease

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    legacy = owner.consumption.transfer.legacy_guard
    plan = owner.consumption.transfer.intent.original_plan
    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(bootstrap.root / "expired-recovery")
    run = PrivateUpgradeStore(runs.root / owner.consumption.transfer.intent.recovery_id)
    original = NativeUpgradeClaim(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        node=legacy.node,
        plan_sha256=plan.authority_sha256(),
        updated_at=owner.claimed_at,
    )
    bootstrap.write("claim.json", original)
    bootstrap.write("runtime.json", legacy.bootstrap_runtime)
    bootstrap.write("status.json", legacy)
    before = (bootstrap.root / "claim.json").read_bytes()
    settings = Settings(upgrade_bootstrap_root=str(bootstrap.root))
    monkeypatch.setattr(worker, "publish_holding_controller", Mock())
    if lost:
        write = PrivateUpgradeStore.write
        fired = []

        def interrupted(self, key, value, *, replace=True):
            write(self, key, value, replace=replace)
            if self.root == bootstrap.root and key == "runtime.json" and not fired:
                fired.append(True)
                raise RuntimeError("fixture lost runtime response")

        monkeypatch.setattr(PrivateUpgradeStore, "write", interrupted)
    with (
        bootstrap.ownership("recovery-transfer.lock") as fd,
        bind_recovery_lease(bootstrap, owner.sha256(), fd),
    ):
        if lost:
            with pytest.raises(RuntimeError):
                worker.publish_unready_support(settings, owner, Mock())
            assert (run.root / "publication.intent.json").exists()
        status = worker.publish_unready_support(settings, owner, Mock())
        assert status.ready is False and status.process == owner.process
        assert worker.publish_unready_support(settings, owner, Mock()) == status
        # Impossible reverse-order partial state is not adopted as our effect.
        bootstrap.write("runtime.json", legacy.bootstrap_runtime)
        with pytest.raises(UpgradeConflict):
            worker.publish_unready_support(settings, owner, Mock())
        bootstrap.write("runtime.json", owner.runtime)
    assert (bootstrap.root / "claim.json").read_bytes() == before
    assert bootstrap.read("runtime.json", type(owner.runtime)) == owner.runtime
