"""Observed retirement is distinct from an acknowledged bootout receipt."""

import pytest
from pydantic import ValidationError
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim


@pytest.mark.parametrize("fault", [None, "late", "source", "scope", "command"])
def test_reconciliation_contract_cannot_copy_or_invent_command_ack(tmp_path, fault):
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryLegacyStopReconciliation

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    receipt = owner.consumption
    data = {
        "consumption": receipt.model_dump(mode="json"),
        "effect": owner.legacy_stop.effect.model_dump(mode="json"),
        "preserved_source": owner.legacy_stop.preserved_source.model_dump(mode="json"),
        "observed_at": owner.claimed_at.isoformat(),
        "service_unloaded": True,
        "legacy_identity_retired": True,
    }
    if fault == "late":
        data["observed_at"] = receipt.transfer.expires_at.isoformat()
    elif fault == "source":
        data["preserved_source"]["process"]["pid"] += 1
    elif fault == "scope":
        data["effect"]["consumption_sha256"] = "b" * 64
    elif fault == "command":
        data["bootout_acknowledged"] = True
    if fault:
        with pytest.raises(ValidationError):
            UpgradeExpiredRecoveryLegacyStopReconciliation.model_validate(data)
    else:
        proof = UpgradeExpiredRecoveryLegacyStopReconciliation.model_validate(data)
        assert "stopped_at" not in proof.model_dump()
        assert proof.effect == owner.legacy_stop.effect
