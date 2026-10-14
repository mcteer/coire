"""Actual lifetime exclusion and lost acceptance evidence, native facts stubbed."""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_upgrade_expired_recovery_capture import retained_native_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdClaim
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    UpgradePhase,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_core.upgrade_recovery import capture_expired_native_recovery
from coire_node.recovery_lease import require_recovery_lease


def cold_fixture(tmp_path, *, supervision="launchd"):
    plan, native, scope, request, now = retained_native_fixture(tmp_path / "graphs")
    request = request.model_copy(update={"recovery_mode": "cold", "supervision": supervision})
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
    original = NativeUpgradeClaim(
        job_id=native.job_id,
        generation=native.generation,
        node=native.source.node,
        plan_sha256=native.plan_sha256,
        updated_at=now,
    )
    status = NativeGuardStatus(
        node=native.source.node,
        bootstrap_runtime=native.source.runtime,
        process=NodeAgentProcessIdentity(
            pid=12345,
            create_time_s=100,
            environment_sha256=native.source.runtime.environment_sha256,
        ),
        selected=native.source,
        ready=False,
        observed_at=now,
    )
    last = NativeNodeProcess(selected=native.source, process=native.source_process, recorded_at=now)
    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(bootstrap.root / "expired-recovery")
    run = PrivateUpgradeStore(runs.root / intent.recovery_id)
    settings = Settings(
        node_name="coire-edge-a", upgrades_enabled=True, upgrade_bootstrap_root=str(bootstrap.root)
    )
    process = NodeAgentProcessIdentity(
        pid=os.getpid(), create_time_s=100, environment_sha256=native.candidate.environment_sha256
    )
    return (
        settings,
        intent,
        native.candidate,
        (original, native, status, last),
        process,
        bootstrap,
        run,
    )


@pytest.mark.parametrize("fault", [None, "lost", "revoked", "changed_ack", "physical", "retained"])
def test_cold_claim_retains_both_locks_and_exact_lost_response_history(
    tmp_path, monkeypatch, fault
):
    from coire_node import expired_recovery_cold_owner as worker

    settings, intent, runtime, preimages, process, bootstrap, run = cold_fixture(tmp_path)
    monkeypatch.setattr(worker, "retained_cold_preimages", lambda *_: preimages)
    monkeypatch.setattr(worker, "verify_passive_support_identity", lambda *_: process)
    monkeypatch.setattr(worker, "recovery_kernel_boot_identity", lambda *_: "synthetic-kernel")
    monkeypatch.setattr(worker, "observe_managed_definition", lambda *_: "d" * 64)
    physical = Mock(
        side_effect=UpgradeUnavailable("fixture physical uncertainty")
        if fault == "physical"
        else None
    )
    monkeypatch.setattr(worker, "verify_cold_physical", physical)
    if fault == "retained":
        run.write(
            "cold.claim.json",
            UpgradeExpiredRecoveryColdClaim(
                intent=intent,
                process=process,
                runtime=runtime,
                kernel_boot_identity="synthetic-kernel",
                original_claim=preimages[0],
                original_native=preimages[1],
                previous_guard=preimages[2],
                last_node=preimages[3],
                supervision="launchd",
                managed_definition_sha256="d" * 64,
                claimed_at=datetime.now(UTC),
            ),
        )
    accepted = []

    def accept(claim):
        accepted.append(claim)
        assert run.read("cold.claim.json", UpgradeExpiredRecoveryColdClaim) == claim
        for key in ["lifetime.lock", "recovery-transfer.lock"]:
            with pytest.raises(UpgradeConflict), bootstrap.ownership(key, wait_s=0):
                pass
        if fault == "lost":
            raise UpgradeUnavailable("fixture lost commit acknowledgement")
        return (
            claim.model_copy(update={"kernel_boot_identity": "foreign"})
            if fault == "changed_ack"
            else claim
        )

    control = SimpleNamespace(
        verify_support_context=Mock(return_value=intent),
        read_cold_recovery_continuation=Mock(return_value=None),
        accept_cold_recovery_owner=Mock(side_effect=accept),
        verify_cold_recovery_owner=Mock(
            side_effect=UpgradeUnavailable("fixture revoked")
            if fault == "revoked"
            else lambda claim: claim
        ),
    )
    before = {p: p.read_bytes() for p in (tmp_path / "graphs").rglob("*.json")}
    if fault:
        with (
            pytest.raises(UpgradeUnavailable),
            worker.initial_cold_owner(settings, intent, runtime, control),
        ):
            pytest.fail("uncertain cold owner must not yield")
        assert run.read("cold.activation.json", UpgradeExpiredRecoveryColdClaim) is None
        if fault in {"lost", "changed_ack", "revoked"}:
            assert run.read("cold.claim.json", UpgradeExpiredRecoveryColdClaim) == accepted[0]
            with (
                pytest.raises(UpgradeUnavailable, match="explicit resume"),
                worker.initial_cold_owner(settings, intent, runtime, control),
            ):
                pass
            assert len(accepted) == 1
    else:
        with worker.initial_cold_owner(settings, intent, runtime, control) as owner:
            require_recovery_lease(bootstrap, owner.sha256())
            assert run.read("cold.activation.json", UpgradeExpiredRecoveryColdClaim) == owner
            for key in ["lifetime.lock", "recovery-transfer.lock"]:
                with pytest.raises(UpgradeConflict), bootstrap.ownership(key, wait_s=0):
                    pass
        with pytest.raises(UpgradeUnavailable):
            require_recovery_lease(bootstrap, owner.sha256())
        with (
            bootstrap.ownership("lifetime.lock", wait_s=0),
            bootstrap.ownership("recovery-transfer.lock", wait_s=0),
        ):
            pass
    assert before == {p: p.read_bytes() for p in (tmp_path / "graphs").rglob("*.json")}
