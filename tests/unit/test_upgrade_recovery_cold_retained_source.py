"""Exact retained Source crosses a new owner epoch without a duplicate spawn."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_continuation_observer import observer_fixture

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryIntent,
)
from coire_core.models.upgrade_recovery_continuation import UpgradeColdContinuationGrant
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    NodeAgentProcessIdentity,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_lease import bind_recovery_lease


def continuation_fixture(tmp_path, monkeypatch, *, archived=False, post_handback=False):
    from test_upgrade_recovery_continuation_observer import archived_observer_fixture

    if post_handback:
        from test_upgrade_post_handback_continuation import post_handback_fixture

        worker, settings, body, control, bootstrap, _old_run, node, _, _ = post_handback_fixture(
            tmp_path, monkeypatch
        )
        with bootstrap.ownership("lifetime.lock"):
            proof = worker.observe_continuation(settings, body, control)
        current = Path(settings.upgrade_environment_root) / "current"
        old_source = proof.previous_source
    else:
        fixture = archived_observer_fixture if archived else observer_fixture
        worker, settings, body, control, bootstrap, _old_run, current, old_source = fixture(
            tmp_path, monkeypatch
        )
        proof = worker.observe_continuation(settings, body, control)
    previous = proof.previous_owner
    now = proof.observed_at + timedelta(seconds=1)
    intent = UpgradeExpiredRecoveryIntent.model_validate(
        previous.intent.model_copy(
            update={
                "recovery_id": "01ARZ3NDEKTSV4RRFFQ69G5FAX",
                "accepted_at": now,
                "deadline_at": now + timedelta(seconds=previous.intent.request.recovery_s),
            }
        ).model_dump(mode="json")
    )
    if post_handback:
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
        node.write("maintenance.json", fence)
    grant = UpgradeColdContinuationGrant(
        grant_id="01ARZ3NDEKTSV4RRFFQ69G5FAY",
        request_sha256="c" * 64,
        intent=intent,
        binding=proof.binding,
        issued_by=intent.accepted_by,
        issued_at=now,
    )
    actor = NodeAgentProcessIdentity(
        pid=555006,
        create_time_s=now.timestamp(),
        environment_sha256=previous.runtime.environment_sha256,
    )
    claim = UpgradeExpiredRecoveryColdClaim(
        intent=intent,
        process=actor,
        runtime=previous.runtime,
        kernel_boot_identity=previous.kernel_boot_identity,
        original_claim=proof.binding.post_handback.released_claim
        if post_handback
        else previous.original_claim,
        original_native=previous.original_native,
        previous_guard=proof.binding.previous_guard,
        last_node=proof.binding.retained_node,
        supervision="fixture",
        retired_supervisor=previous.retired_supervisor,
        claimed_at=now,
        continuation=proof.binding,
    )
    runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
    new_run = PrivateUpgradeStore(runs.path(intent.recovery_id))
    new_run.write("cold.claim.json", claim)
    new_run.write("cold.activation.json", claim)
    if post_handback:
        from coire_core.models.upgrade_recovery_continuation import (
            UpgradeColdSourceKeeperRetirement,
        )

        new_run.write(
            "source.keeper.retirement.receipt.json",
            UpgradeColdSourceKeeperRetirement(
                grant=grant,
                actor=actor,
                started_at=now,
                retired_at=now,
            ),
        )
    from coire_node import expired_recovery_cold_launch as launcher
    from coire_node import expired_recovery_cold_owner as physical
    from coire_node import expired_recovery_cold_source as selector

    for module in (selector, physical, launcher):
        monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(physical, "verify_passive_support_identity", lambda *_: actor)
    source_python = tmp_path / "graphs/envs/source/bin/python3"
    child = SimpleNamespace(cmdline=lambda: [str(source_python), "-I", "-B", "-m", "coire_node"])
    monkeypatch.setattr(physical, "_owned_node_process", lambda *_: child)
    monkeypatch.setattr(launcher, "_owned_node_process", lambda *_: child)
    monkeypatch.setattr(launcher, "verify_prepared_runtime", lambda *_: source_python)
    control.verify_cold_recovery_owner = Mock(side_effect=lambda value: value)
    control.verify_support_context = Mock(return_value=intent)
    control.read_cold_recovery_continuation = Mock(return_value=grant)
    if post_handback:
        inventory = control.local_inventory()
        control.local_inventory = Mock(
            return_value=inventory.model_copy(
                update={
                    "job_id": fence.job_id,
                    "generation": fence.generation,
                    "observed_at": now,
                }
            )
        )
        monkeypatch.setattr(
            physical, "observe_fixture_supervisor", lambda *_: previous.retired_supervisor
        )
    return (
        selector,
        physical,
        launcher,
        settings,
        claim,
        bootstrap,
        new_run,
        current,
        old_source,
        control,
    )


@pytest.mark.parametrize("fault", [None, "lost_receipt", "source_gone"])
def test_recorded_retention_never_spawns_or_recaptures_source_birth(tmp_path, monkeypatch, fault):
    selector, _physical, launcher, settings, claim, bootstrap, run, current, old_source, control = (
        continuation_fixture(tmp_path, monkeypatch)
    )
    spawn = Mock(side_effect=AssertionError("continuation attempted a duplicate Source spawn"))
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    original_birth = old_source.process.create_time_s
    with (
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, claim.sha256(), descriptor),
    ):
        selection = selector.project_cold_source(settings, claim, control)
        assert (
            selection.projection.source_selection
            == old_source.launch.source.projection.source_selection
        )
        selector.publish_cold_source_readiness(settings, selection, control)
        if fault == "source_gone":
            monkeypatch.setattr(launcher, "_owned_node_process", lambda *_: None)
            with pytest.raises(UpgradeUnavailable):
                launcher.launch_cold_source(settings, selection, control)
        else:
            retained = launcher.launch_cold_source(settings, selection, control)
            assert retained.process == old_source.process
            assert retained.process.create_time_s == original_birth
            assert retained.launch.retained_process == old_source.process
            if fault == "lost_receipt":
                run.path("cold.source.process.0.json").unlink()
            replay = launcher.launch_cold_source(settings, selection, control)
            assert replay.process == retained.process
            assert replay.launch == retained.launch
    spawn.assert_not_called()
    assert (
        bootstrap.read("status.json", NativeGuardStatus).selected
        == old_source.launch.source.projection.source_selection
    )
    node = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades").read(
        "node-process.json", NativeNodeProcess
    )
    assert node.process == old_source.process
    assert node.selected == old_source.launch.source.projection.source_selection
    assert (
        current.resolve()
        == (
            Path(settings.upgrade_environment_root)
            / claim.original_native.source.runtime.environment_id
        ).resolve()
    )


@pytest.mark.parametrize("fault", [None, "lost_acceptance"])
def test_initial_continuation_owner_holds_all_actual_exclusion_before_core_accept(
    tmp_path, monkeypatch, fault
):
    from coire_core.errors import UpgradeConflict

    (
        _selector,
        physical,
        _launcher,
        settings,
        expected,
        bootstrap,
        run,
        _current,
        source,
        control,
    ) = continuation_fixture(tmp_path, monkeypatch)
    run.path("cold.claim.json").unlink()
    run.path("cold.activation.json").unlink()
    records = PrivateUpgradeStore(bootstrap.root.parent / "fixture-records", create=False)
    previous = PrivateUpgradeStore(
        run.root.parent / expected.continuation.previous_recovery_id, create=False
    )
    old_bytes = (
        previous.path("cold.claim.json").read_bytes(),
        previous.path("cold.source.process.0.json").read_bytes(),
    )
    received = []

    def accept(claim):
        received.append(claim)
        assert claim.continuation == expected.continuation
        assert claim.last_node.process == source.process
        for store, name in (
            (bootstrap, "lifetime.lock"),
            (bootstrap, "recovery-transfer.lock"),
            (records, "fixture-supervisor.lock"),
        ):
            with pytest.raises(UpgradeConflict), store.ownership(name):
                pass
        if fault:
            raise UpgradeUnavailable("fixture lost acceptance reply")
        return claim

    control.accept_cold_recovery_owner = Mock(side_effect=accept)
    if fault:
        with (
            pytest.raises(UpgradeUnavailable, match="fixture lost"),
            physical.initial_cold_owner(settings, expected.intent, expected.runtime, control),
        ):
            pytest.fail("lost acknowledgement yielded ownership")
        assert run.read("cold.claim.json", UpgradeExpiredRecoveryColdClaim) == received[0]
        assert not run.path("cold.activation.json").exists()
        with (
            pytest.raises(UpgradeUnavailable, match="explicit resume"),
            physical.initial_cold_owner(settings, expected.intent, expected.runtime, control),
        ):
            pytest.fail("lost acknowledgement silently recaptured an owner")
    else:
        with physical.initial_cold_owner(
            settings, expected.intent, expected.runtime, control
        ) as actual:
            assert actual == received[0]
            assert actual.last_node.process == source.process
    assert len(received) == 1
    assert old_bytes == (
        previous.path("cold.claim.json").read_bytes(),
        previous.path("cold.source.process.0.json").read_bytes(),
    )
