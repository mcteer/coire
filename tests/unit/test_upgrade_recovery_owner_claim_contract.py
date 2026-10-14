"""Claim is scoped to one consumed successor and bound stop proof, not absence."""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_upgrade_expired_recovery_transfer import transfer_payload


def owner_claim_payload(tmp_path):
    from coire_core.models.upgrade_recovery import (
        UpgradeExpiredRecoveryLegacyService,
        UpgradeExpiredRecoveryLegacyStopIntent,
        UpgradeExpiredRecoveryLegacyStopReceipt,
        UpgradeExpiredRecoveryOwnerTransfer,
        UpgradeExpiredRecoveryTransferConsumption,
    )
    from coire_core.models.upgrades import NativeNodeProcess

    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    receipt = UpgradeExpiredRecoveryTransferConsumption(
        consumption_id=uuid4(),
        transfer=transfer,
        consumer=transfer.successor,
        consumed_at=transfer.issued_at,
    )
    now = transfer.issued_at + timedelta(milliseconds=1)
    source = NativeNodeProcess(
        selected=transfer.legacy_guard.selected,
        process=transfer.legacy_guard.process.model_copy(update={"pid": 50000}),
        recorded_at=now,
    )
    service = UpgradeExpiredRecoveryLegacyService(
        target="system/com.coire.upgrade-guard",
        owner_uid=501,
        abandon_process_group=True,
        process=transfer.legacy_guard.process,
        observation_sha256="a" * 64,
        observed_at=now,
    )
    effect = UpgradeExpiredRecoveryLegacyStopIntent(
        recovery_id=transfer.intent.recovery_id,
        consumption_sha256=receipt.sha256(),
        legacy_guard=transfer.legacy_guard,
        successor=transfer.successor,
        started_at=now,
        service=service,
        definition_sha256="b" * 64,
        preserved_source=source,
    )
    stop = UpgradeExpiredRecoveryLegacyStopReceipt(
        effect=effect, preserved_source=source, stopped_at=now
    )
    runtime = transfer.legacy_guard.bootstrap_runtime.model_copy(
        update={
            "environment_id": transfer.intent.support_release.manifest.environment_id,
            "environment_sha256": receipt.consumer.environment_sha256,
        }
    )
    body = {
        "consumption": receipt,
        "legacy_stop": stop,
        "process": receipt.consumer,
        "runtime": runtime,
        "kernel_boot_identity": str(uuid4()),
        "claimed_at": now,
    }
    return body


@pytest.mark.parametrize("fault", [None, "foreign_process", "late"])
def test_owner_claim_binds_scope_stop_and_kernel(tmp_path, fault):
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim

    body = owner_claim_payload(tmp_path)
    receipt = body["consumption"]
    transfer = receipt.transfer
    if fault == "late":
        body["claimed_at"] = transfer.expires_at
    elif fault == "foreign_process":
        body["process"] = receipt.consumer.model_copy(update={"pid": receipt.consumer.pid + 1})
    if fault:
        with pytest.raises(ValidationError):
            UpgradeExpiredRecoveryOwnerClaim.model_validate(body)
    else:
        claim = UpgradeExpiredRecoveryOwnerClaim.model_validate(body)
        assert (
            claim.consumption.transfer.intent.original_recovery_deadline_at
            == transfer.intent.original_recovery_deadline_at
        )
