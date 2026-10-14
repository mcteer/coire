"""Real sealed pointer reconciliation with immutable recovery write-ahead evidence."""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_upgrade_recovery_cold_owner import cold_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryColdSourceIntent,
    UpgradeExpiredRecoveryColdSourceReceipt,
    UpgradeRecoveryRetiredSupervisor,
)
from coire_core.models.upgrades import NativeGuardStatus, NativeRecoveryIntent
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_lease import bind_recovery_lease


def source_fixture(tmp_path, monkeypatch, *, activated=True, supervision="launchd"):
    from coire_node import expired_recovery_cold_source as worker

    settings, intent, runtime, preimages, process, bootstrap, run = cold_fixture(
        tmp_path, supervision=supervision
    )
    settings = settings.model_copy(
        update={
            "upgrade_environment_root": str(tmp_path / "graphs/envs"),
            "node_state_dir": str(tmp_path / "state"),
        }
    )
    native = (
        preimages[1].model_copy(
            update={
                "activation_started_at": preimages[1].recovery_started_at,
                "activation_grant_id": uuid4(),
                "readiness_deadline_at": preimages[1].recovery_started_at + timedelta(seconds=120),
            }
        )
        if activated
        else preimages[1]
    )
    claim = UpgradeExpiredRecoveryColdClaim(
        intent=intent,
        runtime=runtime,
        process=process,
        kernel_boot_identity="synthetic-kernel",
        original_claim=preimages[0],
        original_native=native,
        previous_guard=preimages[2],
        last_node=preimages[3],
        supervision=supervision,
        managed_definition_sha256="d" * 64 if supervision == "launchd" else None,
        retired_supervisor=UpgradeRecoveryRetiredSupervisor(
            pid=12348, create_time_s=100, owner_uid=os.getuid(), script_sha256="d" * 64
        )
        if supervision == "fixture"
        else None,
        claimed_at=datetime.now(UTC),
    )
    bootstrap.write("runtime.json", preimages[2].bootstrap_runtime)
    bootstrap.write("claim.json", preimages[0])
    bootstrap.write("status.json", preimages[2])
    operations = PrivateUpgradeStore(bootstrap.root / "operations")
    operation = PrivateUpgradeStore(operations.root / native.job_id)
    guard = PrivateUpgradeStore(operation.root / "guard")
    guard.write("intent.json", native)
    node = PrivateUpgradeStore(tmp_path / "state/upgrades")
    node.write("maintenance.json", intent.original_plan.maintenance)
    node.write("node-process.json", preimages[3])
    run.write("cold.claim.json", claim)
    run.write("cold.activation.json", claim)
    current = tmp_path / "graphs/envs/current"
    current.unlink()
    current.symlink_to(tmp_path / ("graphs/envs/target" if activated else "graphs/envs/source"))
    from coire_node import expired_recovery_cold_owner as physical

    monkeypatch.setattr(physical, "verify_passive_support_identity", lambda *_: process)
    monkeypatch.setattr(physical, "recovery_kernel_boot_identity", lambda *_: "synthetic-kernel")
    monkeypatch.setattr(physical, "verify_retired_identity", lambda *_: None)
    monkeypatch.setattr(physical, "fixed_launchctl_probe", lambda *_: lambda _: None)
    monkeypatch.setattr(physical, "observe_managed_definition", lambda *_: "d" * 64)
    if supervision == "fixture":
        monkeypatch.setattr(
            physical, "observe_fixture_supervisor", lambda *_: claim.retired_supervisor
        )
    monkeypatch.setattr(physical, "native_process_entries", lambda *_: ())
    control = SimpleNamespace(verify_cold_recovery_owner=Mock(side_effect=lambda c: c))
    return worker, settings, claim, bootstrap, run, guard, current, control


def test_pre_activation_source_projection_never_fabricates_old_verification(tmp_path, monkeypatch):
    worker, settings, claim, bootstrap, _, guard, current, control = source_fixture(
        tmp_path, monkeypatch, activated=False
    )
    original = claim.original_native
    with (
        bootstrap.ownership("lifetime.lock"),
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, claim.sha256(), descriptor),
    ):
        receipt = worker.project_cold_source(settings, claim, control)
    assert current.resolve() == tmp_path / "graphs/envs/source"
    projected = guard.read("intent.json", NativeRecoveryIntent)
    assert projected == original.model_copy(
        update={"phase": "prepared", "updated_at": receipt.projection.prepared_at}
    )
    assert projected.activation_started_at is None and projected.activation_grant_id is None
    assert projected.recovery_deadline_at == original.recovery_deadline_at
    assert receipt.projection.source_selection == original.source
    assert projected.phase != "unchanged_verified"


@pytest.mark.parametrize("interruption", [None, "pointer", "native", "bootstrap", "receipt"])
def test_cold_source_projection_reconciles_exact_write_boundaries(
    tmp_path, monkeypatch, interruption
):
    worker, settings, claim, bootstrap, run, guard, current, control = source_fixture(
        tmp_path, monkeypatch
    )
    before = claim.original_native
    original_write = PrivateUpgradeStore.write
    interrupted = False

    def write(store, key, value, **kwargs):
        nonlocal interrupted
        result = original_write(store, key, value, **kwargs)
        match = {
            "pointer": key == "selection.json",
            "native": key == "intent.json" and store.root == guard.root,
            "bootstrap": key == "runtime.json" and store.root == bootstrap.root,
            "receipt": key == "cold.source.receipt.json",
        }
        if interruption and match[interruption] and not interrupted:
            interrupted = True
            raise UpgradeUnavailable("fixture lost response after durable effect")
        return result

    monkeypatch.setattr(PrivateUpgradeStore, "write", write)
    with (
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, claim.sha256(), descriptor),
    ):
        if interruption:
            with pytest.raises(UpgradeUnavailable, match="lost response"):
                worker.project_cold_source(settings, claim, control)
        receipt = worker.project_cold_source(settings, claim, control)
        assert worker.project_cold_source(settings, claim, control) == receipt
        status = worker.publish_cold_source_readiness(settings, receipt, control)
    assert current.resolve() == tmp_path / "graphs/envs/source"
    effect = run.read("cold.source.intent.json", UpgradeExpiredRecoveryColdSourceIntent)
    assert effect.owner.original_native == before
    assert effect.before_pointer == before.candidate
    assert effect.projected_native == before.model_copy(
        update={"phase": "source_selected", "updated_at": effect.prepared_at}
    )
    assert effect.projected_native.recovery_deadline_at == before.recovery_deadline_at
    assert guard.read("intent.json", NativeRecoveryIntent) == effect.projected_native
    assert run.read("cold.source.receipt.json", UpgradeExpiredRecoveryColdSourceReceipt) == receipt
    assert bootstrap.read("status.json", NativeGuardStatus) == status
    assert status.selected == effect.source_selection and status.ready
    with pytest.raises(UpgradeUnavailable, match="lifetime lease"):
        worker.project_cold_source(settings, claim, control)


@pytest.mark.parametrize("fault", ["revoked", "unknown_pointer", "unknown_native", "released"])
def test_cold_source_refuses_unknown_or_revoked_preimages(tmp_path, monkeypatch, fault):
    worker, settings, claim, bootstrap, run, guard, current, control = source_fixture(
        tmp_path, monkeypatch
    )
    if fault == "revoked":
        control.verify_cold_recovery_owner.side_effect = UpgradeUnavailable("fixture revoked")
    elif fault == "unknown_pointer":
        current.unlink()
        current.symlink_to(tmp_path)
    elif fault == "unknown_native":
        guard.write(
            "intent.json",
            claim.original_native.model_copy(update={"updated_at": datetime.now(UTC)}),
        )
    else:
        node = PrivateUpgradeStore(tmp_path / "state/upgrades")
        node.write(
            "maintenance.json",
            claim.intent.original_plan.maintenance.model_copy(update={"state": "released"}),
        )
    prior = current.readlink()
    with (
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, claim.sha256(), descriptor),
        pytest.raises((UpgradeConflict, UpgradeUnavailable)),
    ):
        worker.project_cold_source(settings, claim, control)
    assert current.readlink() == prior
    assert run.read("cold.source.intent.json", UpgradeExpiredRecoveryColdSourceIntent) is None
    assert guard.read("intent.json", NativeRecoveryIntent).phase == "recovery_required"
