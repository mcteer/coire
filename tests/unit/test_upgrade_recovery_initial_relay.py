"""Transient initial samples cannot become or renew administrative authority."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_upgrade_expired_recovery_capture import retained_native_fixture

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryObservation
from coire_core.models.upgrades import NativeUpgradeClaim, UpgradePhase


def observation(tmp_path):
    plan, native, scope, request, now = retained_native_fixture(tmp_path)
    return UpgradeExpiredRecoveryObservation(
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


def test_replay_detachment_expiry_and_backward_clock(tmp_path, monkeypatch):
    from coire_api.upgrades import expired_recovery_initial_relay as module

    proof = observation(tmp_path)
    now = [proof.observed_at]
    tick = [100.0]
    monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda zone: now[0]))
    monkeypatch.setattr(module.time, "monotonic", lambda: tick[0])
    relay = module.InitialRecoveryObservationRelay()
    relay.publish(proof)
    first = relay.read(proof.request, proof.support)
    assert first == proof and first is not proof
    assert relay.read(proof.request, proof.support) is not first
    tick[0] += 4
    relay.publish(proof)  # Exact replay cannot refresh retained time.
    tick[0] += 2
    now[0] -= timedelta(days=1)
    with pytest.raises(UpgradeUnavailable):
        relay.read(proof.request, proof.support)
    with pytest.raises(UpgradeUnavailable):
        relay.publish(proof)


@pytest.mark.parametrize("fault", ["future", "stale", "changed", "regressed", "missing"])
def test_initial_cache_refuses_bad_samples_and_preserves_prior(tmp_path, fault):
    from coire_api.upgrades.expired_recovery_initial_relay import InitialRecoveryObservationRelay

    proof = observation(tmp_path)
    relay = InitialRecoveryObservationRelay()
    relay.publish(proof)
    if fault == "missing":
        with pytest.raises(UpgradeUnavailable):
            relay.read(
                proof.request.model_copy(update={"idempotency_key": "foreign"}), proof.support
            )
    else:
        changed = (
            proof.model_copy(
                update={
                    "observed_at": proof.observed_at
                    + timedelta(seconds=10 if fault == "future" else -10)
                }
            )
            if fault in {"future", "stale"}
            else proof.model_copy(
                update={"observed_at": proof.observed_at - timedelta(milliseconds=1)}
            )
            if fault == "regressed"
            else proof.model_copy(
                update={
                    "native": proof.native.model_copy(
                        update={"updated_at": proof.native.updated_at + timedelta(seconds=1)}
                    )
                }
            )
        )
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
            relay.publish(changed)
    assert relay.read(proof.request, proof.support) == proof


@pytest.mark.parametrize("fault", [None, "node", "slot", "phase", "catalog", "missing"])
async def test_ingress_rechecks_original_and_catalog_without_admin_effect(
    tmp_path, monkeypatch, fault
):
    from coire_api.upgrades import expired_recovery_initial_relay as module

    proof = observation(tmp_path)
    op = proof.plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED})
    row = SimpleNamespace(
        active_slot=None if fault == "slot" else 1,
        phase="verifying" if fault == "phase" else "recovery_required",
        payload=op.model_dump(mode="json"),
        execution_plan=proof.plan.model_dump(mode="json"),
    )
    session = SimpleNamespace(get=AsyncMock(return_value=None if fault == "missing" else row))
    monkeypatch.setattr(module, "operation_lock", AsyncMock())
    monkeypatch.setattr(
        module,
        "get_release",
        AsyncMock(return_value=proof.plan.source if fault == "catalog" else proof.support),
    )
    if fault:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable, UpgradeForbidden)):
            await module.verify_observation_context(
                session, proof, "coire-edge-b" if fault == "node" else proof.plan.operation.node
            )
    else:
        await module.verify_observation_context(session, proof, proof.plan.operation.node)
