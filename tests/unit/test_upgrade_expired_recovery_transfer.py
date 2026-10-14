"""Recovery owner handover has a distinct short-lived, exact-bound intent."""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_upgrade_expired_recovery_capture import retained_native_fixture


@pytest.mark.parametrize("fault", [None, "late", "foreign_process"])
def test_consumption_receipt_binds_captured_transfer_window(tmp_path, fault):
    from coire_core.models.upgrade_recovery import (
        UpgradeExpiredRecoveryOwnerTransfer,
        UpgradeExpiredRecoveryTransferConsumption,
    )

    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    body = {
        "consumption_id": uuid4(),
        "transfer": transfer,
        "consumer": transfer.successor,
        "consumed_at": transfer.issued_at + timedelta(seconds=1),
    }
    if fault == "late":
        body["consumed_at"] = transfer.expires_at
    elif fault == "foreign_process":
        body["consumer"] = transfer.successor.model_copy(update={"pid": transfer.successor.pid + 1})
    if fault:
        with pytest.raises(ValidationError):
            UpgradeExpiredRecoveryTransferConsumption.model_validate(body)
    else:
        receipt = UpgradeExpiredRecoveryTransferConsumption.model_validate(body)
        assert receipt.transfer == transfer
        assert receipt.consumer == transfer.successor


def transfer_payload(tmp_path):
    from coire_core.models.upgrades import NativeGuardStatus, NodeAgentProcessIdentity, UpgradePhase
    from coire_core.upgrade_recovery import capture_expired_native_recovery

    plan, native, scope, request, now = retained_native_fixture(tmp_path)
    intent = capture_expired_native_recovery(
        request=request,
        operation=plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED}),
        plan=plan,
        native=native,
        support=plan.target,
        scopes=(scope,),
        recovery_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        accepted_by=uuid4(),
        now=now,
    )
    legacy = NativeGuardStatus(
        node=native.source.node,
        bootstrap_runtime=native.source.runtime,
        process=NodeAgentProcessIdentity(
            pid=12345,
            create_time_s=100.0,
            environment_sha256=native.source.runtime.environment_sha256,
        ),
        selected=native.source,
        observed_at=now,
    )
    successor = NodeAgentProcessIdentity(
        pid=12346, create_time_s=101.0, environment_sha256=native.candidate.environment_sha256
    )
    return {
        "transfer_id": uuid4(),
        "intent": intent,
        "legacy_guard": legacy,
        "successor": successor,
        "issued_at": now,
        "expires_at": now + timedelta(seconds=30),
    }


@pytest.mark.parametrize("fault", [None, "expired", "same_owner", "foreign_runtime"])
def test_transfer_intent_bounds_owner_and_new_epoch(tmp_path, fault):
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerTransfer

    body = transfer_payload(tmp_path)
    if fault == "expired":
        body["expires_at"] += timedelta(seconds=1)
    elif fault == "same_owner":
        body["successor"] = body["successor"].model_copy(
            update={"pid": body["legacy_guard"].process.pid}
        )
    elif fault == "foreign_runtime":
        body["successor"] = body["successor"].model_copy(update={"environment_sha256": "f" * 64})
    if fault:
        with pytest.raises(ValidationError):
            UpgradeExpiredRecoveryOwnerTransfer.model_validate(body)
    else:
        value = UpgradeExpiredRecoveryOwnerTransfer.model_validate(body)
        assert value.expires_at < value.intent.deadline_at
        assert value.legacy_guard == body["legacy_guard"]
