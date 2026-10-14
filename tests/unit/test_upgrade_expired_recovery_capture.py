"""Capture a new epoch only from matching expired, retained native authority."""

from datetime import timedelta
from uuid import uuid4

import pytest
from upgrade_recovery_fixtures import retained_native_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    UpgradePhase,
)


def test_expired_source_verified_is_not_retroactive_completion(tmp_path):
    from coire_core.upgrade_recovery import capture_expired_native_recovery

    plan, native, scope, request, now = retained_native_fixture(tmp_path)
    started = native.recovery_started_at
    native = native.model_copy(
        update={
            "phase": "source_verified",
            "activation_started_at": started,
            "activation_grant_id": uuid4(),
            "readiness_deadline_at": started + timedelta(seconds=120),
        }
    )
    operation = plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED})
    intent = capture_expired_native_recovery(
        request=request,
        operation=operation,
        plan=plan,
        native=native,
        support=plan.target,
        scopes=(scope,),
        recovery_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        accepted_by=uuid4(),
        now=now,
    )
    assert intent.original_recovery_deadline_at == native.recovery_deadline_at
    assert operation.phase is UpgradePhase.RECOVERY_REQUIRED
    assert native.phase == "source_verified"


@pytest.mark.parametrize("retained_dispatch", [False, True])
def test_pre_dispatch_capture_requires_positively_matching_empty_history(
    tmp_path, retained_dispatch
):
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryRequest
    from coire_core.upgrade_recovery import capture_expired_native_recovery

    plan, native, scope, request, now = retained_native_fixture(tmp_path)
    request = UpgradeExpiredRecoveryRequest.model_validate(
        request.model_dump(mode="json") | {"original_scope_sha256s": []}
    )
    operation = plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED})
    args = {
        "request": request,
        "operation": operation,
        "plan": plan,
        "native": native,
        "support": plan.target,
        "scopes": (scope,) if retained_dispatch else (),
        "recovery_id": "01ARZ3NDEKTSV4RRFFQ69G5FAW",
        "accepted_by": uuid4(),
        "now": now,
    }
    if retained_dispatch:
        with pytest.raises(UpgradeConflict, match="dispatch scope differs"):
            capture_expired_native_recovery(**args)
    else:
        captured = capture_expired_native_recovery(**args)
        assert captured.request.original_scope_sha256s == ()
        assert captured.original_recovery_deadline_at == native.recovery_deadline_at
        assert captured.original_plan == plan


@pytest.mark.parametrize("fault", [None, "unexpired", "generation", "phase", "scope"])
def test_capture_requires_actual_matching_expired_native_state(tmp_path, fault):
    from coire_core.upgrade_recovery import capture_expired_native_recovery

    plan, native, scope, request, now = retained_native_fixture(tmp_path)
    operation = plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED})
    if fault == "unexpired":
        native = native.model_copy(
            update={
                "recovery_started_at": now,
                "recovery_deadline_at": now + timedelta(seconds=300),
            }
        )
    elif fault == "generation":
        native = native.model_copy(update={"generation": native.generation + 1})
    elif fault == "phase":
        operation = operation.model_copy(update={"phase": UpgradePhase.ACTIVATING})
    elif fault == "scope":
        request = request.model_copy(update={"original_scope_sha256s": ("f" * 64,)})
    args = {
        "request": request,
        "operation": operation,
        "plan": plan,
        "native": native,
        "support": plan.target,
        "scopes": (scope,),
        "recovery_id": "01ARZ3NDEKTSV4RRFFQ69G5FAW",
        "accepted_by": uuid4(),
        "now": now,
    }
    if fault:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
            capture_expired_native_recovery(**args)
    else:
        intent = capture_expired_native_recovery(**args)
        assert intent.original_recovery_deadline_at == native.recovery_deadline_at
        assert intent.deadline_at == now + timedelta(seconds=300)
        assert native.phase == "recovery_required"
        assert operation.phase is UpgradePhase.RECOVERY_REQUIRED
