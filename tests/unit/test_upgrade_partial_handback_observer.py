"""Real journal/kernel exclusion; ready native child facts are explicit fixtures."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_cold_handback import perform_source_handback_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationContext,
    UpgradeColdContinuationObserverInput,
    UpgradeColdContinuationRead,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "fault", [None, "changed_helper", "unknown_actor", "later_record", "not_ready", "changed_data"]
)
def test_partial_ready_handback_is_metadata_only_and_keeps_original_deadlines(
    tmp_path, monkeypatch, fault
):
    from coire_node import expired_recovery_partial_handback as worker

    captured = []

    def capture(settings, owner, bootstrap, run, receipt, control):
        captured.append((settings, owner, bootstrap, run, receipt, control))

    perform_source_handback_fixture(tmp_path, monkeypatch, callback=capture, supervision="fixture")
    settings, owner, bootstrap, run, receipt, control = captured[0]
    handback = receipt.helper.handback
    source = handback.verification.request.smoke.smoke.source
    proof = handback.verification
    run.path("cold.handback.receipt.json").unlink()
    run.write("cold.source.process.0.json", source)
    run.write("cold.smoke.command.json", proof.request.smoke.smoke)
    run.write("cold.smoke.receipt.json", proof.request.smoke)
    now = owner.intent.deadline_at + timedelta(seconds=1)
    context = UpgradeColdContinuationContext(
        intent=owner.intent,
        owner=owner,
        previous_smoke_command=proof.request.smoke.smoke,
        previous_handback_intent=handback,
        data_baseline=proof.data_before,
        native_baseline=proof.native_before,
        current_data=proof.data_before.model_copy(update={"observed_at": now}),
        observed_at=now,
    )
    python = tmp_path / "graphs/envs/source/bin/python3"
    monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(worker, "verify_retired_identity", lambda *_: None)
    monkeypatch.setattr(worker, "observe_fixture_supervisor", lambda *_: owner.retired_supervisor)
    monkeypatch.setattr(
        worker, "recovery_kernel_boot_identity", lambda *_: owner.kernel_boot_identity
    )
    monkeypatch.setattr(worker, "verify_prepared_runtime", lambda *_: python)
    node = Mock()
    node.cmdline.return_value = [str(python), "-I", "-B", "-m", "coire_node"]
    guard = Mock()
    guard.cmdline.return_value = [str(python), "-I", "-B", "-m", "coire_node.upgrade_guard"]
    monkeypatch.setattr(worker, "_owned_node_process", lambda *_: node)
    monkeypatch.setattr(worker, "_owned_native_process", lambda *_: guard)
    monkeypatch.setattr(
        worker,
        "native_process_entries",
        lambda *_: (
            ()
            if fault != "unknown_actor"
            else (SimpleNamespace(process_id=4444, process_created_at=1),)
        ),
    )
    native = proof.native_before.model_copy(
        update={"observed_at": now, "total_bytes": 1 if fault == "changed_data" else 0}
    )
    monkeypatch.setattr(worker, "capture_native_data", lambda *_args, **_kwargs: native)
    status = receipt.status.model_copy(update={"observed_at": now, "ready": fault != "not_ready"})
    bootstrap.write("status.json", status)
    PrivateUpgradeStore(tmp_path / "state/upgrades").write(
        "maintenance.json", owner.intent.local_maintenance().model_copy(update={"state": "active"})
    )
    if fault == "changed_helper":
        run.write(
            "cold.handback.helper.json",
            receipt.helper.model_copy(
                update={"process": receipt.helper.process.model_copy(update={"pid": 7777})}
            ),
        )
    if fault == "later_record":
        run.path("cold.supervision.intent.json").write_text("{}")
    for key in ["local_inventory", "peer_inventory"]:
        old = getattr(control, key)()
        getattr(control, key).side_effect = None
        getattr(control, key).return_value = old.model_copy(
            update={
                "observed_at": now,
                "owners": tuple(o.model_copy(update={"entries": ()}) for o in old.owners),
            }
        )
    control._read = Mock(side_effect=lambda *_args, **kwargs: kwargs["payload"])
    body = UpgradeColdContinuationObserverInput(
        previous_recovery_id=owner.intent.recovery_id,
        read=UpgradeColdContinuationRead(
            previous_intent_sha256=owner.intent.sha256(), previous_owner_sha256=owner.sha256()
        ),
        observer_release=owner.intent.support_release,
        observer_runtime=owner.runtime,
    )
    actor = NodeAgentProcessIdentity(
        pid=345678,
        create_time_s=now.timestamp(),
        environment_sha256=owner.runtime.environment_sha256,
    )
    originals = {
        path: path.read_bytes()
        for path in [
            bootstrap.path("claim.json"),
            bootstrap.path("runtime.json"),
            run.path("cold.handback.intent.json"),
            run.path("cold.verification.json"),
        ]
    }
    with bootstrap.ownership("lifetime.lock"):
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable, ValueError)):
                worker.observe_partial_handback(
                    settings,
                    body,
                    control,
                    context,
                    actor,
                    lambda: context,
                    now + timedelta(seconds=15),
                )
        else:
            observed = worker.observe_partial_handback(
                settings,
                body,
                control,
                context,
                actor,
                lambda: context,
                now + timedelta(seconds=15),
            )
            assert observed.binding.post_handback.stage == "ready_unreceipted"
            assert observed.binding.previous_deadline_at == owner.intent.deadline_at
            assert observed.binding.post_handback.source_helper.observed_at == now
            assert observed.previous_unreceipted_helper == receipt.helper
    assert {path: path.read_bytes() for path in originals} == originals
    assert not run.path("cold.handback.receipt.json").exists()
