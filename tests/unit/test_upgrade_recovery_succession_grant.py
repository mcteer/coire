"""Succession handover window never recaptures the recovery budget."""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionObservation,
)


@pytest.mark.parametrize(
    "fault", [None, "long", "stale", "future", "recovery_expiry", "credential"]
)
def test_succession_grant_window_is_bounded_by_original_intent(tmp_path, fault):
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoverySuccessionGrant

    previous = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    sampled = previous.claimed_at + timedelta(seconds=1)
    proof = UpgradeExpiredRecoverySuccessionObservation(
        succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        previous_owner=previous,
        successor=previous.process.model_copy(update={"pid": previous.process.pid + 1}),
        kernel_boot_identity=previous.kernel_boot_identity,
        preserved_source=previous.legacy_stop.preserved_source,
        observed_at=sampled,
        previous_owner_retired=True,
    )
    data = {
        "observation": proof.model_dump(mode="json"),
        "accepted_at": sampled.isoformat(),
        "expires_at": (sampled + timedelta(seconds=10)).isoformat(),
        "user_id": str(uuid4()),
        "key_id": None,
        "credential_version": None,
    }
    if fault == "long":
        data["expires_at"] = (sampled + timedelta(seconds=31)).isoformat()
    elif fault == "stale":
        data["accepted_at"] = (sampled + timedelta(seconds=6)).isoformat()
    elif fault == "future":
        data["accepted_at"] = (sampled - timedelta(seconds=1)).isoformat()
    elif fault == "recovery_expiry":
        data["expires_at"] = (
            previous.consumption.transfer.intent.deadline_at + timedelta(seconds=1)
        ).isoformat()
    elif fault == "credential":
        data["key_id"] = str(uuid4())
    if fault:
        with pytest.raises(ValidationError):
            UpgradeExpiredRecoverySuccessionGrant.model_validate(data)
    else:
        grant = UpgradeExpiredRecoverySuccessionGrant.model_validate(data)
        assert grant.observation.previous_owner == previous
        assert grant.expires_at <= previous.consumption.transfer.intent.deadline_at
