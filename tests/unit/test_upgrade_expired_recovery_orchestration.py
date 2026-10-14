"""Core must close snapshot transaction before bounded trusted observation I/O."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_upgrade_expired_recovery_capture import retained_native_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    RECOVERY_PRESERVATION_TABLES,
    UpgradeExpiredRecoveryDataSnapshot,
    UpgradeExpiredRecoveryObservation,
)
from coire_core.models.upgrades import NativeUpgradeClaim, UpgradePhase


@pytest.mark.parametrize("fault", ["phase", "generation", "source"])
async def test_snapshot_refuses_foreign_or_nonrecoverable_authority_before_observer(
    tmp_path, monkeypatch, fault
):
    from coire_api.upgrades import expired_recovery_orchestration as service

    plan, native, _scope, request, _now = retained_native_fixture(tmp_path)
    operation = plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED})
    if fault == "phase":
        operation = operation.model_copy(update={"phase": UpgradePhase.ACTIVATING})
    elif fault == "generation":
        request = request.model_copy(update={"original_generation": native.generation + 1})
    else:
        request = request.model_copy(update={"expected_source_sha256": "f" * 64})
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        get=AsyncMock(
            return_value=SimpleNamespace(
                active_slot=1,
                phase=operation.phase.value,
                payload=operation.model_dump(mode="json"),
                execution_plan=plan.model_dump(mode="json"),
            )
        ),
    )
    monkeypatch.setattr(service, "authorize", AsyncMock(return_value=operation.user_id))
    monkeypatch.setattr(service, "operation_lock", AsyncMock())
    catalog = AsyncMock(return_value=plan.target)
    monkeypatch.setattr(service, "get_release", catalog)
    with pytest.raises(UpgradeConflict):
        await service.load_context(session, object(), request)
    catalog.assert_not_awaited()


@pytest.mark.parametrize("fault", [None, "stale"])
async def test_observation_io_is_outside_transaction_and_budget_captured_once(
    tmp_path, monkeypatch, fault
):
    from coire_api.upgrades import expired_recovery_orchestration as service

    plan, native, scope, request, now = retained_native_fixture(tmp_path)
    operation = plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED})
    observation = UpgradeExpiredRecoveryObservation(
        request=request,
        plan=plan,
        native=native,
        claim=NativeUpgradeClaim(
            job_id=native.job_id,
            generation=native.generation,
            node=native.source.node,
            plan_sha256=native.plan_sha256,
            updated_at=now,
        ),
        scopes=(scope,),
        support=plan.target,
        observer_runtime=native.candidate,
        observed_at=now,
    )
    if fault:
        observation = observation.model_copy(update={"observed_at": now - timedelta(seconds=6)})
    active = []

    @asynccontextmanager
    async def transaction():
        active.append(True)
        try:
            yield object()
        finally:
            active.pop()

    class Sessions:
        begin = staticmethod(transaction)

    async def observe(request_arg, support_arg):
        assert not active
        assert request_arg == request and support_arg == plan.target
        return observation

    monkeypatch.setattr(
        service,
        "load_context",
        AsyncMock(return_value=(plan.operation.user_id, None, operation, plan, plan.target)),
    )
    from coire_api.upgrades import expired_recovery_data

    async def capture(session, intent):
        assert active  # Server data baseline commits with acceptance and audit.
        return UpgradeExpiredRecoveryDataSnapshot(
            recovery_id=intent.recovery_id,
            original_job_id=intent.request.original_job_id,
            original_generation=intent.request.original_generation,
            source_sha256=intent.request.expected_source_sha256,
            table_sha256s=dict.fromkeys(RECOVERY_PRESERVATION_TABLES, "a" * 64),
            row_counts=dict.fromkeys(RECOVERY_PRESERVATION_TABLES, 0),
            observed_at=datetime.now(UTC),
        )

    baseline = AsyncMock(side_effect=capture)
    monkeypatch.setattr(expired_recovery_data, "capture_data_snapshot", baseline)
    accept = AsyncMock(side_effect=lambda session, principal, intent, **kw: (intent, True))
    monkeypatch.setattr(service, "accept_expired_recovery", accept)
    if fault:
        with pytest.raises(UpgradeUnavailable):
            await service.accept_with_observation(Sessions(), object(), request, observe)
        accept.assert_not_awaited()
        baseline.assert_not_awaited()
    else:
        intent, created = await service.accept_with_observation(
            Sessions(), object(), request, observe
        )
        assert created
        assert intent.original_recovery_deadline_at == native.recovery_deadline_at
        assert intent.deadline_at > datetime.now(UTC)
        assert accept.await_count == 1
        baseline.assert_awaited_once()
        assert accept.await_args.kwargs["data_baseline"].recovery_id == intent.recovery_id
