"""Crash succession observation is not absence-based ownership or budget renewal."""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim


@pytest.mark.parametrize("fault", [None, "same", "boot", "late", "source", "deadline"])
def test_same_boot_succession_observation_preserves_original_authority(tmp_path, fault):
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoverySuccessionObservation

    previous = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    data = {
        "succession_id": "01ARZ3NDEKTSV4RRFFQ69G5FAX",
        "previous_owner": previous.model_dump(mode="json"),
        "successor": previous.process.model_copy(
            update={"pid": previous.process.pid + 1}
        ).model_dump(mode="json"),
        "kernel_boot_identity": previous.kernel_boot_identity,
        "preserved_source": previous.legacy_stop.preserved_source.model_dump(mode="json"),
        "observed_at": (previous.claimed_at + timedelta(seconds=1)).isoformat(),
        "previous_owner_retired": True,
    }
    if fault == "same":
        data["successor"] = previous.process.model_dump(mode="json")
    elif fault == "boot":
        data["kernel_boot_identity"] = "different-kernel"
    elif fault == "late":
        data["observed_at"] = previous.consumption.transfer.intent.deadline_at.isoformat()
    elif fault == "source":
        data["preserved_source"]["process"]["pid"] += 1
    elif fault == "deadline":
        data["deadline_at"] = "2099-01-01T00:00:00Z"
    if fault:
        with pytest.raises(ValidationError):
            UpgradeExpiredRecoverySuccessionObservation.model_validate(data)
    else:
        proof = UpgradeExpiredRecoverySuccessionObservation.model_validate(data)
        assert proof.previous_owner == previous
        assert "deadline_at" not in proof.model_dump()
