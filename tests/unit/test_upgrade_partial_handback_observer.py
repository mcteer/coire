"""Real journal/kernel exclusion; ready native child facts are explicit fixtures."""

from contextlib import nullcontext
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
    "fault",
    [
        None,
        "changed_helper",
        "unknown_actor",
        "later_record",
        "not_ready",
        "changed_data",
        "keeper_retired",
        "keeper_unstarted_again",
        "keeper_unknown",
        "keeper_fence_effect",
    ],
)
def test_partial_ready_handback_is_metadata_only_and_keeps_original_deadlines(
    tmp_path, monkeypatch, fault, capture_success=None
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
        "maintenance.json", owner.intent.local_maintenance().model_copy(update={"state": "fencing"})
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
    keeper_case = fault in {
        "keeper_retired",
        "keeper_unstarted_again",
        "keeper_unknown",
        "keeper_fence_effect",
    }
    if keeper_case:
        from coire_node import expired_recovery_unstarted_lineage as custody_reader

        monkeypatch.setattr(custody_reader, "verify_retired_identity", lambda *_: None)
        from coire_core.models.upgrade_recovery import (
            UpgradeExpiredRecoveryIntent,
            UpgradeExpiredRecoverySupportLaunchIntent,
            UpgradeExpiredRecoverySupportProcess,
            UpgradeExpiredRecoverySupportStartup,
        )
        from coire_core.models.upgrade_recovery_continuation import (
            UpgradeColdContinuationGrant,
            UpgradeColdSourceKeeperRetirement,
        )
        from coire_core.models.upgrades import NodeMaintenanceCommand, UpgradeMaintenance

        with bootstrap.ownership("lifetime.lock"):
            live = worker.observe_partial_handback(
                settings,
                body,
                control,
                context,
                actor,
                lambda: context,
                now + timedelta(seconds=15),
            )
        child_id = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
        deadline = now + timedelta(seconds=300)
        fence = UpgradeMaintenance(
            job_id=child_id,
            generation=owner.original_claim.generation + 1,
            targets=(settings.node_name,),
            state="active",
            updated_at=now,
            drain_deadline_at=deadline,
            initiator_user_id=owner.intent.accepted_by,
        )
        child = UpgradeExpiredRecoveryIntent.model_validate(
            owner.intent.model_dump(mode="json")
            | {
                "recovery_id": child_id,
                "accepted_at": now,
                "deadline_at": deadline,
                "local_recovery_maintenance": fence.model_dump(mode="json"),
            }
        )
        grant = UpgradeColdContinuationGrant(
            grant_id="01ARZ3NDEKTSV4RRFFQ69G5FAY",
            request_sha256="a" * 64,
            intent=child,
            binding=live.binding,
            issued_by=child.accepted_by,
            issued_at=now,
        )
        passive = NodeAgentProcessIdentity(
            pid=98770,
            create_time_s=now.timestamp(),
            environment_sha256=owner.runtime.environment_sha256,
        )
        startup = UpgradeExpiredRecoverySupportStartup(
            intent=child, runtime=owner.runtime, prepared_at=now
        )
        launched = UpgradeExpiredRecoverySupportLaunchIntent(
            startup=startup, launcher=passive.model_copy(update={"pid": 98771}), started_at=now
        )
        accepted = UpgradeExpiredRecoverySupportProcess(
            intent=child, runtime=owner.runtime, process=passive, observed_at=now
        )
        marker = UpgradeColdSourceKeeperRetirement(grant=grant, actor=passive, started_at=now)
        retired = marker.model_copy(update={"retired_at": now + timedelta(seconds=1)})
        custodian = PrivateUpgradeStore(bootstrap.root / "expired-recovery" / child_id)
        for key, value in [
            ("startup.json", startup),
            ("support.launch.intent.json", launched),
            ("support.launch.receipt.json", accepted),
            ("source.keeper.retirement.intent.json", marker),
            ("source.keeper.retirement.receipt.json", retired),
            (
                "local.previous.released.maintenance.json",
                live.binding.post_handback.released_maintenance,
            ),
            ("local.recovery.fence.intent.json", NodeMaintenanceCommand(maintenance=fence)),
        ]:
            custodian.write(key, value)
        if fault == "keeper_unknown":
            custodian.path("unknown.json").write_text("{}")
        if fault == "keeper_fence_effect":
            custodian.path("local.recovery.fence.receipt.json").write_text("{}")
        now = deadline + timedelta(seconds=1)
        context = context.model_copy(
            update={
                "unstarted_intent": child,
                "observed_at": now,
                "current_data": context.current_data.model_copy(update={"observed_at": now}),
            }
        )
        native = native.model_copy(update={"observed_at": now})
        if fault == "keeper_unstarted_again":
            late = live.binding.post_handback.model_copy(
                update={
                    "stage": "keeper_retired",
                    "keeper_custodian_recovery_id": child_id,
                    "keeper_retirement_sha256": retired.sha256(),
                }
            )
            retained_binding = live.binding.model_copy(
                update={"observed_at": now, "post_handback": late}
            )
            second_id = "01ARZ3NDEKTSV4RRFFQ69G5FB1"
            second_fence = fence.model_copy(
                update={
                    "job_id": second_id,
                    "generation": fence.generation + 1,
                    "updated_at": now,
                    "drain_deadline_at": now + timedelta(seconds=300),
                }
            )
            second = UpgradeExpiredRecoveryIntent.model_validate(
                child.model_dump(mode="json")
                | {
                    "recovery_id": second_id,
                    "accepted_at": now,
                    "deadline_at": now + timedelta(seconds=300),
                    "local_recovery_maintenance": second_fence.model_dump(mode="json"),
                }
            )
            second_grant = UpgradeColdContinuationGrant(
                grant_id="01ARZ3NDEKTSV4RRFFQ69G5FB2",
                request_sha256="b" * 64,
                intent=second,
                binding=retained_binding,
                unstarted_startup=accepted,
                issued_by=second.accepted_by,
                issued_at=now,
            )
            second_startup = UpgradeExpiredRecoverySupportStartup(
                intent=second, runtime=owner.runtime, prepared_at=now
            )
            second_actor = passive.model_copy(
                update={"pid": 98772, "create_time_s": now.timestamp()}
            )
            second_launch = UpgradeExpiredRecoverySupportLaunchIntent(
                startup=second_startup,
                launcher=second_actor.model_copy(update={"pid": 98773}),
                started_at=now,
            )
            second_receipt = UpgradeExpiredRecoverySupportProcess(
                intent=second, runtime=owner.runtime, process=second_actor, observed_at=now
            )
            second_run = PrivateUpgradeStore(bootstrap.root / "expired-recovery" / second_id)
            for key, value in [
                ("startup.json", second_startup),
                ("support.launch.intent.json", second_launch),
                ("support.launch.receipt.json", second_receipt),
            ]:
                second_run.write(key, value)
            now = second.deadline_at + timedelta(seconds=1)
            context = context.model_copy(
                update={
                    "unstarted_intent": second,
                    "unstarted_grant": second_grant,
                    "observed_at": now,
                    "current_data": context.current_data.model_copy(update={"observed_at": now}),
                }
            )
            native = native.model_copy(update={"observed_at": now})
        actor = actor.model_copy(update={"create_time_s": now.timestamp()})
        monkeypatch.setattr(worker, "_owned_native_process", lambda *_: None)
        for key in ["local_inventory", "peer_inventory"]:
            old = getattr(control, key).return_value
            getattr(control, key).return_value = old.model_copy(update={"observed_at": now})
    originals = {
        path: path.read_bytes()
        for path in [
            bootstrap.path("claim.json"),
            bootstrap.path("runtime.json"),
            run.path("cold.handback.intent.json"),
            run.path("cold.verification.json"),
        ]
    }
    with nullcontext() if keeper_case else bootstrap.ownership("lifetime.lock"):
        if fault not in {None, "keeper_retired", "keeper_unstarted_again"}:
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
            assert observed.binding.post_handback.stage == (
                "keeper_retired" if keeper_case else "ready_unreceipted"
            )
            assert observed.binding.previous_deadline_at == owner.intent.deadline_at
            assert observed.binding.post_handback.source_helper.observed_at == status.observed_at
            assert observed.previous_unreceipted_helper == receipt.helper
            if capture_success is not None:
                capture_success(
                    settings, owner, bootstrap, run, source, observed, context, body, actor, control
                )
    assert {path: path.read_bytes() for path in originals} == originals
    assert not run.path("cold.handback.receipt.json").exists()
