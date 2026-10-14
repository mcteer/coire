"""Owner retains observed retirement as distinct proof, never bootout success."""

from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.upgrade_journal import PrivateUpgradeStore


def test_reconciled_owner_preserves_separate_proof_shape(tmp_path):
    from coire_node.expired_recovery_effect_journal import retained_legacy_retirement_proof

    payload = owner_claim_payload(tmp_path)
    original = UpgradeExpiredRecoveryOwnerClaim.model_validate(payload)
    payload = original.model_dump(mode="json")
    observed = {
        "consumption": payload["consumption"],
        "effect": payload["legacy_stop"]["effect"],
        "preserved_source": payload["legacy_stop"]["preserved_source"],
        "observed_at": payload["legacy_stop"]["stopped_at"],
        "service_unloaded": True,
        "legacy_identity_retired": True,
    }
    payload["legacy_stop"] = observed
    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(payload)
    run = PrivateUpgradeStore(tmp_path / "scope")
    run.write("consumption.json", owner.consumption)
    run.write("legacy-stop.intent.json", owner.legacy_stop.effect)
    run.write("legacy-stop.reconciliation.json", owner.legacy_stop)
    assert retained_legacy_retirement_proof(run, owner.consumption) == owner.legacy_stop
    assert "stopped_at" not in owner.legacy_stop.model_dump()
    assert original.sha256() != owner.sha256()
