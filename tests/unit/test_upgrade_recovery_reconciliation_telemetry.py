"""Bounded outcome telemetry carries IDs, never command-success or secret data."""

import logging
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryLegacyStopReconciliation,
    UpgradeExpiredRecoveryOwnerClaim,
)
from coire_core.settings import Settings


@pytest.mark.parametrize("refused", [False, True])
def test_retirement_telemetry_bounded_outcome_linkage(tmp_path, monkeypatch, caplog, refused):
    from coire_node import expired_recovery_stop_reconciliation as worker

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    receipt = owner.consumption
    proof = UpgradeExpiredRecoveryLegacyStopReconciliation(
        consumption=receipt,
        effect=owner.legacy_stop.effect,
        preserved_source=owner.legacy_stop.preserved_source,
        observed_at=owner.claimed_at,
        service_unloaded=True,
        legacy_identity_retired=True,
    )
    monkeypatch.setattr(
        worker,
        "_reconcile_legacy_stop",
        Mock(
            return_value=proof,
            side_effect=UpgradeUnavailable("fixture refusal") if refused else None,
        ),
    )
    counter = Mock()
    monkeypatch.setattr(worker, "reconciliation_events", counter)
    with caplog.at_level(logging.INFO, logger=worker.__name__):
        if refused:
            with pytest.raises(UpgradeUnavailable):
                worker.reconcile_legacy_stop(Settings(), receipt, owner.runtime, Mock())
        else:
            assert worker.reconcile_legacy_stop(Settings(), receipt, owner.runtime, Mock()) == proof
    counter.add.assert_called_once_with(1, {"outcome": "refused" if refused else "observed"})
    record = next(r for r in caplog.records if r.name == worker.__name__)
    assert record.job_id == receipt.transfer.intent.request.original_job_id
    assert record.recovery_id == receipt.transfer.intent.recovery_id
    assert record.generation == receipt.transfer.intent.original_plan.operation.generation
    assert record.exc_info is None
