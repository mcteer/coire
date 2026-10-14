"""Exact released handback metadata: real exclusion and immutable private history."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest
from test_upgrade_recovery_cold_handback import perform_source_handback_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClosureIntent,
    UpgradeExpiredRecoveryColdSupervisionIntent,
    UpgradeExpiredRecoveryColdSupervisionReceipt,
)
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationContext,
    UpgradeColdContinuationObserverInput,
    UpgradeColdContinuationRead,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity
from coire_core.upgrade_journal import PrivateUpgradeStore


def post_handback_fixture(tmp_path, monkeypatch, *, inherited=False):
    real_process = psutil.Process
    captured = {}

    def capture(settings, owner, bootstrap, run, handback, control):
        captured.update(
            settings=settings,
            owner=owner,
            bootstrap=bootstrap,
            run=run,
            handback=handback,
            control=control,
        )

    perform_source_handback_fixture(
        tmp_path,
        monkeypatch,
        callback=capture,
        supervision="fixture",
        continuation=inherited,
        inherited=inherited,
    )
    from coire_node import expired_recovery_continuation_observer as worker

    monkeypatch.setattr(worker.psutil, "Process", real_process)

    settings, owner, bootstrap, run, handback, control = (
        captured[k] for k in ("settings", "owner", "bootstrap", "run", "handback", "control")
    )
    when = handback.observed_at + timedelta(seconds=1)
    supervision = UpgradeExpiredRecoveryColdSupervisionReceipt(
        supervision=UpgradeExpiredRecoveryColdSupervisionIntent(handback=handback, started_at=when),
        process=NodeAgentProcessIdentity(
            pid=76543,
            create_time_s=when.timestamp(),
            environment_sha256=owner.runtime.environment_sha256,
        ),
        kind="fixture",
        observed_at=when,
    )
    run.write("cold.supervision.intent.json", supervision.supervision)
    run.write("cold.supervision.receipt.json", supervision)
    node = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades")
    from coire_core.models.upgrades import UpgradeMaintenance

    before = node.read("maintenance.json", UpgradeMaintenance)
    closure = UpgradeExpiredRecoveryColdClosureIntent(
        supervision=supervision, before_maintenance=before, started_at=when
    )
    run.write("cold.closure.intent.json", closure)
    node.write(
        "maintenance.json", before.model_copy(update={"state": "released", "updated_at": when})
    )
    now = owner.intent.deadline_at + timedelta(seconds=1)
    bootstrap.write("status.json", handback.status.model_copy(update={"observed_at": now}))
    PrivateUpgradeStore(bootstrap.root.parent / "fixture-records")
    proof = handback.helper.handback.verification
    source = proof.request.smoke.smoke.source
    run.write("cold.source.receipt.json", source.launch.source)
    run.write("cold.source.launch.0.json", source.launch)
    run.write("cold.source.process.0.json", source)
    run.write("cold.smoke.command.json", proof.request.smoke.smoke)
    run.write("cold.smoke.receipt.json", proof.request.smoke)
    if proof.request.pair is not None:
        for result in proof.request.pair.results:
            run.write(f"cold.pair.{result.dispatch.index}.command.json", result.dispatch)
            run.write(f"cold.pair.{result.dispatch.index}.result.json", result)
        run.write("cold.pair.qualification.json", proof.request.pair)
    run.write("cold.verification.request.json", proof.request)
    context = UpgradeColdContinuationContext(
        intent=owner.intent,
        owner=owner,
        previous_smoke_command=proof.request.smoke.smoke,
        previous_handback=handback,
        data_baseline=proof.data_before,
        native_baseline=proof.native_before,
        current_data=proof.data_before.model_copy(update={"observed_at": now}),
        observed_at=now,
    )
    envs = Path(settings.upgrade_environment_root)
    python = envs / owner.runtime.environment_id / "bin/python3"
    source_python = envs / owner.original_native.source.runtime.environment_id / "bin/python3"
    monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(
        worker,
        "sys",
        SimpleNamespace(
            platform="darwin", prefix=str(python.parent.parent), executable=str(python)
        ),
    )
    monkeypatch.setattr(worker.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(worker.platform, "node", lambda: "coire-edge-a")
    monkeypatch.setattr(
        worker,
        "verify_prepared_runtime",
        lambda _e, _i, rt: source_python if rt == owner.original_native.source.runtime else python,
    )
    guard = SimpleNamespace(
        cmdline=lambda: [str(source_python), "-I", "-B", "-m", "coire_node.upgrade_guard"]
    )
    monkeypatch.setattr(worker, "_owned_native_process", lambda *_: guard)
    monkeypatch.setattr(
        worker,
        "_owned_node_process",
        lambda *_: SimpleNamespace(
            cmdline=lambda: [str(source_python), "-I", "-B", "-m", "coire_node"]
        ),
    )
    monkeypatch.setattr(worker, "verify_retired_identity", lambda *_: None)
    monkeypatch.setattr(worker, "observe_fixture_supervisor", lambda *_: owner.retired_supervisor)
    monkeypatch.setattr(
        worker, "recovery_kernel_boot_identity", lambda *_: owner.kernel_boot_identity
    )
    monkeypatch.setattr(worker, "native_process_entries", lambda *_: ())
    monkeypatch.setattr(
        worker,
        "capture_native_data",
        lambda *_a, **_k: proof.native_before.model_copy(update={"observed_at": now}),
    )
    for name, inventory in (("local_inventory", proof.local), ("peer_inventory", proof.peer)):
        setattr(control, name, Mock(return_value=inventory.model_copy(update={"observed_at": now})))
    control._read = Mock(
        side_effect=lambda _p, model, *_a, **kw: (
            context if model is UpgradeColdContinuationContext else kw["payload"]
        )
    )
    body = UpgradeColdContinuationObserverInput(
        previous_recovery_id=owner.intent.recovery_id,
        read=UpgradeColdContinuationRead(
            previous_intent_sha256=owner.intent.sha256(), previous_owner_sha256=owner.sha256()
        ),
        observer_release=owner.intent.support_release,
        observer_runtime=owner.runtime,
    )
    return worker, settings, body, control, bootstrap, run, node, supervision, handback


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "foreign_claim",
        "foreign_fence",
        "supervisor_live",
        "missing_handback",
        "missing_closure",
        "guard_absent",
    ],
)
@pytest.mark.parametrize("inherited", [False, True])
def test_post_handback_observation_requires_exact_released_history_and_live_writer(
    tmp_path, monkeypatch, fault, inherited
):
    worker, settings, body, control, bootstrap, run, node, supervision, handback = (
        post_handback_fixture(tmp_path, monkeypatch, inherited=inherited)
    )
    from coire_core.models.upgrades import NativeUpgradeClaim, UpgradeMaintenance

    if fault == "foreign_claim":
        claim = bootstrap.read("claim.json", NativeUpgradeClaim)
        bootstrap.write("claim.json", claim.model_copy(update={"generation": claim.generation + 1}))
    elif fault == "foreign_fence":
        fence = node.read("maintenance.json", UpgradeMaintenance)
        node.write(
            "maintenance.json", fence.model_copy(update={"generation": fence.generation + 1})
        )
    elif fault == "supervisor_live":

        def retired(identity):
            if identity == supervision.process:
                raise UpgradeUnavailable("supervisor still live")

        monkeypatch.setattr(worker, "verify_retired_identity", retired)
    elif fault == "missing_handback":
        run.path("cold.handback.receipt.json").unlink()
    elif fault == "missing_closure":
        run.path("cold.closure.intent.json").unlink()
    elif fault == "guard_absent":
        monkeypatch.setattr(worker, "_owned_native_process", lambda *_: None)
    before = {p.name: p.read_bytes() for p in run.root.glob("*.json")}
    with bootstrap.ownership("lifetime.lock"):
        if fault is not None:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                worker.observe_continuation(settings, body, control)
        else:
            result = worker.observe_continuation(settings, body, control)
            late = result.binding.post_handback
            assert late.handback_sha256 == handback.sha256()
            assert late.supervision_sha256 == supervision.sha256()
            assert late.released_claim.state == "released"
            assert late.released_maintenance == node.read("maintenance.json", UpgradeMaintenance)
            assert result.binding.previous_guard.process == handback.helper.process
    assert {key: run.path(key).read_bytes() for key in before} == before


@pytest.mark.parametrize("fault", [None, "effect", "support_live"])
def test_post_handback_observer_binds_expired_unstarted_custodian(tmp_path, monkeypatch, fault):
    from coire_core.models.upgrade_recovery import (
        UpgradeExpiredRecoverySupportLaunchIntent,
        UpgradeExpiredRecoverySupportProcess,
        UpgradeExpiredRecoverySupportStartup,
    )
    from coire_node import expired_recovery_unstarted_lineage as lineage

    worker, settings, body, control, bootstrap, _, _, _, _ = post_handback_fixture(
        tmp_path, monkeypatch
    )
    context = control._read("fixture", UpgradeColdContinuationContext)
    failed = context.intent.model_copy(update={"recovery_id": "01ARZ3NDEKTSV4RRFFQ69G5FAZ"})
    context = context.model_copy(update={"unstarted_intent": failed})
    control._read = Mock(
        side_effect=lambda _p, model, *_a, **kw: (
            context if model is UpgradeColdContinuationContext else kw["payload"]
        )
    )
    run = PrivateUpgradeStore(bootstrap.path("expired-recovery") / failed.recovery_id)
    startup = UpgradeExpiredRecoverySupportStartup(
        intent=failed, runtime=context.owner.runtime, prepared_at=failed.accepted_at
    )
    actor = context.owner.process.model_copy(update={"pid": context.owner.process.pid + 20})
    launch = UpgradeExpiredRecoverySupportLaunchIntent(
        startup=startup,
        launcher=actor.model_copy(update={"pid": actor.pid + 1}),
        started_at=failed.accepted_at,
    )
    receipt = UpgradeExpiredRecoverySupportProcess(
        intent=failed, runtime=context.owner.runtime, process=actor, observed_at=failed.accepted_at
    )
    for key, value in (
        ("startup.json", startup),
        ("support.launch.intent.json", launch),
        ("support.launch.receipt.json", receipt),
    ):
        run.write(key, value)
    if fault == "effect":
        run.write("cold.claim.json", context.owner)
    monkeypatch.setattr(
        lineage,
        "verify_retired_identity",
        Mock(side_effect=UpgradeUnavailable("still live") if fault == "support_live" else None),
    )
    with bootstrap.ownership("lifetime.lock"):
        if fault is not None:
            with pytest.raises((UpgradeUnavailable, UpgradeConflict)):
                worker.observe_continuation(settings, body, control)
        else:
            assert worker.observe_continuation(settings, body, control).unstarted_startup == receipt


@pytest.mark.parametrize("lost_ack", [False, True])
def test_post_handback_owner_transfers_actual_writer_and_keeps_released_authority(
    tmp_path, monkeypatch, lost_ack
):
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryIntent
    from coire_core.models.upgrade_recovery_continuation import UpgradeColdContinuationGrant
    from coire_node import expired_recovery_cold_owner as physical
    from coire_node import expired_recovery_cold_source as selector
    from coire_node import expired_recovery_source_keeper_retirement as keeper

    observer, settings, body, control, bootstrap, old_run, node, _, handback = (
        post_handback_fixture(tmp_path, monkeypatch)
    )
    writer = bootstrap.ownership("lifetime.lock")
    writer.__enter__()
    live = True
    try:
        proof = observer.observe_continuation(settings, body, control)
        now = proof.observed_at + timedelta(seconds=1)
        intent = UpgradeExpiredRecoveryIntent.model_validate(
            proof.previous_owner.intent.model_dump(mode="json")
            | {
                "recovery_id": "01ARZ3NDEKTSV4RRFFQ69G5FAX",
                "accepted_at": now,
                "deadline_at": now + timedelta(seconds=300),
            }
        )
        fence = intent.original_plan.maintenance.model_copy(
            update={
                "job_id": intent.recovery_id,
                "generation": intent.request.original_generation + 1,
                "targets": (intent.original_plan.operation.node,),
                "state": "active",
                "updated_at": now,
                "drain_deadline_at": intent.deadline_at,
                "initiator_user_id": intent.accepted_by,
                "acknowledged_generations": {},
            }
        )
        intent = UpgradeExpiredRecoveryIntent.model_validate(
            intent.model_dump(mode="json")
            | {"local_recovery_maintenance": fence.model_dump(mode="json")}
        )
        grant = UpgradeColdContinuationGrant(
            grant_id="01ARZ3NDEKTSV4RRFFQ69G5FAY",
            request_sha256="c" * 64,
            intent=intent,
            binding=proof.binding,
            issued_by=intent.accepted_by,
            issued_at=now,
        )
        run = PrivateUpgradeStore(old_run.root.parent / intent.recovery_id)
        actor = NodeAgentProcessIdentity(
            pid=555006,
            create_time_s=now.timestamp(),
            environment_sha256=proof.previous_owner.runtime.environment_sha256,
        )
        python = (
            Path(settings.upgrade_environment_root)
            / handback.status.selected.runtime.environment_id
            / "bin/python3"
        )
        guard = Mock()
        guard.cmdline.return_value = [str(python), "-I", "-B", "-m", "coire_node.upgrade_guard"]

        def terminate():
            nonlocal live
            live = False
            writer.__exit__(None, None, None)

        guard.terminate.side_effect = terminate
        for module in (physical, keeper, selector):
            monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda _: now))
        for module in (physical, keeper):
            monkeypatch.setattr(module, "verify_passive_support_identity", lambda *_: actor)
            monkeypatch.setattr(module, "verify_prepared_runtime", lambda *_: python)
            monkeypatch.setattr(
                module,
                "_owned_node_process",
                lambda *_: SimpleNamespace(
                    cmdline=lambda: [str(python), "-I", "-B", "-m", "coire_node"]
                ),
            )
        monkeypatch.setattr(keeper, "_owned_native_process", lambda *_: guard if live else None)

        def retired(identity):
            if identity == handback.helper.process and live:
                raise UpgradeUnavailable("Source writer still live")

        monkeypatch.setattr(keeper, "verify_retired_identity", retired)
        monkeypatch.setattr(physical, "verify_retired_identity", retired)
        monkeypatch.setattr(
            physical,
            "observe_fixture_supervisor",
            lambda *_: proof.previous_owner.retired_supervisor,
        )
        control.read_cold_recovery_continuation = Mock(return_value=grant)
        control.verify_support_context = Mock(return_value=intent)
        control.verify_cold_recovery_owner = Mock(side_effect=lambda value: value)

        def accept(claim):
            assert not live
            for store, name in (
                (bootstrap, "lifetime.lock"),
                (bootstrap, "recovery-transfer.lock"),
            ):
                with pytest.raises(UpgradeConflict), store.ownership(name):
                    pass
            assert claim.original_claim == proof.binding.post_handback.released_claim
            if lost_ack:
                raise UpgradeUnavailable("lost acceptance")
            return claim

        control.accept_cold_recovery_owner = Mock(side_effect=accept)
        claim_bytes = bootstrap.path("claim.json").read_bytes()
        fence_bytes = node.path("maintenance.json").read_bytes()
        old_bytes = {p.name: p.read_bytes() for p in old_run.root.glob("*.json")}
        if lost_ack:
            with (
                pytest.raises(UpgradeUnavailable, match="lost acceptance"),
                physical.initial_cold_owner(
                    settings, intent, proof.previous_owner.runtime, control
                ),
            ):
                pytest.fail("lost acceptance yielded ownership")
            assert run.path("cold.claim.json").exists()
            assert not run.path("cold.activation.json").exists()
        else:
            with physical.initial_cold_owner(
                settings, intent, proof.previous_owner.runtime, control
            ) as owner:
                selected = selector.project_cold_source(settings, owner, control)
                assert selected.projection.source_selection == proof.binding.retained_node.selected
        assert guard.terminate.call_count == 1
        assert bootstrap.path("claim.json").read_bytes() == claim_bytes
        assert node.path("maintenance.json").read_bytes() != fence_bytes
        assert run.path("local.previous.released.maintenance.json").read_bytes() == fence_bytes
        assert {key: old_run.path(key).read_bytes() for key in old_bytes} == old_bytes
    finally:
        if live:
            writer.__exit__(None, None, None)
