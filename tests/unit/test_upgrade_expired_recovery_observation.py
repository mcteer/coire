"""Passive retained observations are scoped metadata, never qualification."""

import json

import pytest
from pydantic import ValidationError
from test_upgrade_expired_recovery_capture import retained_native_fixture

from coire_core.models.upgrades import NativeUpgradeClaim


@pytest.mark.parametrize("fault", [None, "released", "generation", "scope", "support"])
def test_observation_binds_claim_native_plan_scopes_and_support(tmp_path, fault):
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryObservation

    plan, native, scope, request, now = retained_native_fixture(tmp_path)
    claim = NativeUpgradeClaim(
        job_id=native.job_id,
        generation=native.generation,
        node=native.source.node,
        plan_sha256=native.plan_sha256,
        updated_at=now,
    )
    if fault == "released":
        claim = claim.model_copy(update={"state": "released"})
    elif fault == "generation":
        claim = claim.model_copy(update={"generation": native.generation + 1})
    elif fault == "scope":
        request = request.model_copy(update={"original_scope_sha256s": ("f" * 64,)})
    elif fault == "support":
        request = request.model_copy(update={"support_release_id": plan.source.release_id})
    body = {
        "request": request.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "native": native.model_dump(mode="json"),
        "claim": claim.model_dump(mode="json"),
        "scopes": [scope.model_dump(mode="json")],
        "support": plan.target.model_dump(mode="json"),
        "observer_runtime": native.candidate.model_dump(mode="json"),
        "observed_at": now.isoformat(),
    }
    if fault:
        with pytest.raises(ValidationError):
            UpgradeExpiredRecoveryObservation.model_validate_json(json.dumps(body))
    else:
        value = UpgradeExpiredRecoveryObservation.model_validate_json(json.dumps(body))
        assert value.native == native
        assert (
            UpgradeExpiredRecoveryObservation.model_validate_json(value.model_dump_json()) == value
        )
