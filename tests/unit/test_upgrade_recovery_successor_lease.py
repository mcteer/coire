"""Actual private exclusion; physical/Core facts remain mocked fixtures."""

import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionGrant,
    UpgradeExpiredRecoverySuccessionObservation,
    UpgradeExpiredRecoverySuccessorClaim,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_lease import require_recovery_lease


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "core",
        "kernel",
        "source",
        "process",
        "alive",
        "late_process",
        "late_initial_grant",
        "late_initial_binding",
        "late_initial_monotonic_binding",
        "lost_ack",
        "session_success",
    ],
)
def test_resumed_successor_holds_actual_lifetime_exclusion(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_successor as module

    original = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    now = original.claimed_at + timedelta(seconds=1)
    proof = UpgradeExpiredRecoverySuccessionObservation(
        succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        previous_owner=original,
        successor=original.process.model_copy(update={"pid": original.process.pid + 1}),
        kernel_boot_identity=original.kernel_boot_identity,
        preserved_source=original.legacy_stop.preserved_source,
        observed_at=now,
        previous_owner_retired=True,
    )
    grant = UpgradeExpiredRecoverySuccessionGrant(
        observation=proof,
        user_id=original.consumption.transfer.intent.accepted_by,
        key_id=None,
        credential_version=None,
        accepted_at=now,
        expires_at=now + timedelta(seconds=10),
    )
    claim = UpgradeExpiredRecoverySuccessorClaim(
        grant=grant,
        process=proof.successor,
        runtime=original.runtime,
        kernel_boot_identity=original.kernel_boot_identity,
        preserved_source=proof.preserved_source,
        claimed_at=now,
    )
    settings = Settings(
        upgrade_bootstrap_root=str(tmp_path / "boot"),
        upgrades_enabled=True,
        node_name=original.consumption.transfer.intent.original_plan.operation.node,
        node_control_host="127.0.0.1",
        core_control_host="core.private",
        node_token=SecretStr("fixture-token"),
    )
    bootstrap = PrivateUpgradeStore(tmp_path / "boot")
    runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"))
    run = PrivateUpgradeStore(runs.path(original.consumption.transfer.intent.recovery_id))
    run.write("owner.claim.json", original, replace=False)
    before = run.path("owner.claim.json").read_bytes()
    monkeypatch.setattr(
        module, "datetime", SimpleNamespace(now=lambda zone: now + timedelta(seconds=11))
    )
    changed = [fault == "process"]
    monkeypatch.setattr(
        module,
        "verify_passive_support_identity",
        lambda *_: original.process if changed[0] else claim.process,
    )
    monkeypatch.setattr(
        module,
        "recovery_kernel_boot_identity",
        lambda *_: "different" if fault == "kernel" else claim.kernel_boot_identity,
    )
    monkeypatch.setattr(
        module, "source_identity", lambda *_: None if fault == "source" else claim.preserved_source
    )
    monkeypatch.setattr(module, "fixed_launchctl_probe", lambda *_: lambda _: None)

    def retired(_):
        if fault == "alive":
            raise UpgradeUnavailable("predecessor live")

    monkeypatch.setattr(module, "verify_retired_identity", retired)
    control = SimpleNamespace(
        verify_recovery_successor=Mock(
            return_value=claim,
            side_effect=UpgradeUnavailable("Core revoked") if fault == "core" else None,
        )
    )
    if fault in {"late_initial_grant", "late_initial_binding", "late_initial_monotonic_binding"}:
        from coire_node import expired_recovery_successor_controller as controller

        observations = Mock()
        monkeypatch.setattr(controller, "observations", observations)
        clock = [now]
        tick = [0.0]
        monkeypatch.setattr(time, "monotonic", lambda: tick[0])
        monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda zone: clock[0]))
        control.probe_recovery_successor = Mock(
            return_value=SimpleNamespace(owner=None, claim_sha256=claim.sha256())
        )
        control.accept_recovery_successor = Mock(return_value=claim)
        probes = [0]

        def delayed_service_probe(*args):
            probes[0] += 1
            if probes[0] == 2 and fault == "late_initial_grant":
                clock[0] = claim.grant.expires_at
            return None

        monkeypatch.setattr(module, "fixed_launchctl_probe", lambda *_: delayed_service_probe)
        if fault in {"late_initial_binding", "late_initial_monotonic_binding"}:
            bind = module.bind_recovery_lease

            @contextmanager
            def delayed_binding(*args):
                with bind(*args):
                    if fault == "late_initial_binding":
                        clock[0] = claim.grant.expires_at
                    else:
                        tick[0] = 10.0  # UTC frozen; short grant budget fully consumed.
                    yield

            monkeypatch.setattr(module, "bind_recovery_lease", delayed_binding)
        with (
            pytest.raises(UpgradeUnavailable),
            module.initial_successor_owner(settings, claim, control),
        ):
            pytest.fail("expired initial grant yielded")
        assert not run.path(f"successor.{proof.succession_id}.claim.json").exists()
        control.accept_recovery_successor.assert_not_called()
        if fault in {"late_initial_binding", "late_initial_monotonic_binding"}:
            observations.add.assert_called_once_with(
                1, {"outcome": "refused", "node": settings.node_name}
            )
        assert run.path("owner.claim.json").read_bytes() == before
        return
    if fault == "late_process":
        calls = [0]

        def late_core(_):
            calls[0] += 1
            if calls[0] == 2:
                changed[0] = True
            return claim

        control.verify_recovery_successor.side_effect = late_core
    with pytest.raises(UpgradeUnavailable):
        module.require_live_successor(settings, claim, control)
    control.verify_recovery_successor.assert_not_called()
    if fault == "late_process":
        with pytest.raises(UpgradeUnavailable):
            module.verify_current_successor(settings, claim, control)
        assert run.path("owner.claim.json").read_bytes() == before
        return
    if fault not in {None, "lost_ack", "session_success"}:
        with (
            pytest.raises(UpgradeUnavailable),
            module.resumed_successor_owner(settings, claim, control),
        ):
            pytest.fail("uncertain successor yielded")
    else:
        with module.resumed_successor_owner(settings, claim, control) as current:
            assert current == claim
            require_recovery_lease(bootstrap, claim.sha256())
            with pytest.raises(UpgradeConflict), bootstrap.ownership("recovery-transfer.lock"):
                pytest.fail("concurrent owner acquired")
            module.require_live_successor(settings, claim, control)
            control.verify_recovery_successor.side_effect = UpgradeUnavailable(
                "Core revoked while held"
            )
            with pytest.raises(UpgradeUnavailable):
                module.require_live_successor(settings, claim, control)
        with pytest.raises(UpgradeUnavailable):
            require_recovery_lease(bootstrap, claim.sha256())
        monkeypatch.setattr(
            module,
            "datetime",
            SimpleNamespace(now=lambda zone: claim.claimed_at + timedelta(seconds=1)),
        )
        control.verify_recovery_successor.side_effect = None
        control.probe_recovery_successor = Mock(
            return_value=SimpleNamespace(owner=None, claim_sha256=claim.sha256())
        )

        def accept(current):
            require_recovery_lease(bootstrap, current.sha256())
            if fault == "lost_ack":
                control.probe_recovery_successor.return_value = SimpleNamespace(
                    owner=current, claim_sha256=current.sha256()
                )
                raise UpgradeUnavailable("Commit acknowledgement lost")
            return current

        control.accept_recovery_successor = Mock(side_effect=accept)
        if fault == "session_success":
            from coire_node import expired_recovery_successor_controller as controller
            from coire_node import expired_recovery_successor_session as session

            monkeypatch.setattr(
                session, "datetime", SimpleNamespace(now=lambda zone: claim.claimed_at)
            )
            monkeypatch.setattr(
                session, "wait_for_succession_grant", Mock(return_value=claim.grant)
            )

            def held(settings, current, transport, stopped):
                assert current == claim and transport is control
                require_recovery_lease(bootstrap, claim.sha256())
                with pytest.raises(UpgradeConflict), bootstrap.ownership("recovery-transfer.lock"):
                    pytest.fail("concurrent acquisition succeeded")

            holding = Mock(side_effect=held)
            monkeypatch.setattr(controller, "run_held_successor", holding)
            session.run_initial_successor_session(
                settings, original, proof.succession_id, control, threading.Event()
            )
            holding.assert_called_once()
            control.accept_recovery_successor.assert_called_once_with(claim)
            with pytest.raises(UpgradeUnavailable):
                require_recovery_lease(bootstrap, claim.sha256())
            assert run.path("owner.claim.json").read_bytes() == before
            return
        if fault == "lost_ack":
            from coire_node import expired_recovery_successor_session as session

            monkeypatch.setattr(
                session, "datetime", SimpleNamespace(now=lambda zone: claim.claimed_at)
            )
            passive = Mock(return_value=claim.grant)
            monkeypatch.setattr(session, "wait_for_succession_grant", passive)
            with pytest.raises(UpgradeUnavailable):
                session.run_initial_successor_session(
                    settings, original, proof.succession_id, control, threading.Event()
                )
            passive.assert_called_once()
            control.accept_recovery_successor.assert_called_once_with(claim)
            name = f"successor.{proof.succession_id}.claim.json"
            retained = run.path(name).read_bytes()
            with (
                pytest.raises(UpgradeConflict),
                module.initial_successor_owner(settings, claim, control),
            ):
                pytest.fail("blind initial retry yielded")
            assert control.probe_recovery_successor(claim).owner == claim
            monkeypatch.setattr(
                module,
                "datetime",
                SimpleNamespace(now=lambda zone: claim.grant.expires_at + timedelta(seconds=1)),
            )
            with module.resumed_successor_owner(settings, claim, control) as resumed:
                assert resumed == claim
                require_recovery_lease(bootstrap, claim.sha256())
            assert run.path(name).read_bytes() == retained
        else:
            with module.initial_successor_owner(settings, claim, control) as accepted:
                assert accepted == claim
                require_recovery_lease(bootstrap, claim.sha256())
                from coire_core.models.upgrades import (
                    NativeGuardStatus,
                    NativeUpgradeClaim,
                    PreparedNativeRuntime,
                )
                from coire_node import expired_recovery_successor_publication as publication

                plan = original.consumption.transfer.intent.original_plan
                legacy = original.consumption.transfer.legacy_guard
                source_claim = NativeUpgradeClaim(
                    job_id=plan.operation.job_id,
                    generation=plan.operation.generation,
                    node=legacy.node,
                    plan_sha256=plan.authority_sha256(),
                    updated_at=original.claimed_at,
                )
                bootstrap.write("claim.json", source_claim)
                bootstrap.write("runtime.json", legacy.bootstrap_runtime)
                bootstrap.write("status.json", legacy)
                monkeypatch.setattr(
                    publication,
                    "datetime",
                    SimpleNamespace(now=lambda zone: claim.claimed_at + timedelta(seconds=1)),
                )
                cancelled_publication = threading.Event()
                real_publication_gate = publication.require_live_successor

                def cancel_publication(*args):
                    real_publication_gate(*args)
                    cancelled_publication.set()

                with monkeypatch.context() as cancellation:
                    cancellation.setattr(publication, "require_live_successor", cancel_publication)
                    with pytest.raises(UpgradeUnavailable):
                        publication.publish_successor_unready(
                            settings, accepted, control, stopped=cancelled_publication
                        )
                assert (
                    bootstrap.read("runtime.json", PreparedNativeRuntime)
                    == legacy.bootstrap_runtime
                )
                assert bootstrap.read("status.json", NativeGuardStatus) == legacy
                original_write = PrivateUpgradeStore.write
                journal_name = f"successor.{proof.succession_id}.publication.json"
                for boundary in (journal_name, "status.json"):
                    with monkeypatch.context() as crash:

                        def interrupted_write(store, name, value, *, boundary=boundary, **kwargs):
                            if (
                                name == boundary
                                and boundary == "status.json"
                                and store.root == bootstrap.root
                            ):
                                raise RuntimeError("status write interrupted")
                            original_write(store, name, value, **kwargs)
                            if name == boundary and boundary == journal_name:
                                raise RuntimeError("journal acknowledgement lost")

                        crash.setattr(PrivateUpgradeStore, "write", interrupted_write)
                        with pytest.raises(RuntimeError):
                            publication.publish_successor_unready(settings, accepted, control)
                    assert bootstrap.read("claim.json", NativeUpgradeClaim) == source_claim
                    assert bootstrap.read("status.json", NativeGuardStatus) == legacy
                published = publication.publish_successor_unready(settings, accepted, control)
                assert published.process == claim.process and published.ready is False
                assert bootstrap.read("claim.json", NativeUpgradeClaim) == source_claim
                assert bootstrap.read("runtime.json", PreparedNativeRuntime) == claim.runtime
                assert bootstrap.read("status.json", NativeGuardStatus) == published
                assert (
                    publication.publish_successor_unready(settings, accepted, control) == published
                )
                from coire_core.models.upgrade_recovery import UpgradeExpiredRecoverySupportStartup
                from coire_node import expired_recovery_successor_controller as controller
                from coire_node import expired_recovery_support as support

                run.write(
                    "startup.json",
                    UpgradeExpiredRecoverySupportStartup(
                        intent=original.consumption.transfer.intent,
                        runtime=claim.runtime,
                        prepared_at=original.claimed_at,
                    ),
                )
                configured = settings.model_copy(
                    update={
                        "upgrade_recovery_id": original.consumption.transfer.intent.recovery_id,
                        "upgrade_recovery_succession_id": proof.succession_id,
                    }
                )
                with monkeypatch.context() as selection:
                    selected = Mock()
                    initial_mode = Mock()
                    selection.setattr(controller, "run_successor_holding", selected)
                    selection.setattr(support, "run_support_mode", initial_mode)
                    support.run_configured_support(configured, threading.Event())
                    assert selected.call_args.args[1] == claim
                    selected.assert_called_once()
                    initial_mode.assert_not_called()
                    selected.reset_mock()
                    missing = configured.model_copy(
                        update={"upgrade_recovery_succession_id": "01ARZ3NDEKTSV4RRFFQ69G5FAY"}
                    )
                    with pytest.raises(UpgradeUnavailable):
                        support.run_configured_support(missing, threading.Event())
                    selected.assert_not_called()
                    initial_mode.assert_not_called()
                    selected.side_effect = UpgradeUnavailable("successor refused")
                    with pytest.raises(UpgradeUnavailable):
                        support.run_configured_support(configured, threading.Event())
                    initial_mode.assert_not_called()
                    selected.reset_mock()
                    cancelled_start = threading.Event()
                    cancelled_start.set()
                    support.run_configured_support(configured, cancelled_start)
                    selected.assert_not_called()
                    initial_mode.assert_not_called()

                monkeypatch.setattr(
                    controller,
                    "datetime",
                    SimpleNamespace(now=lambda zone: claim.claimed_at + timedelta(seconds=1)),
                )
                stopped = threading.Event()
                monkeypatch.setattr(stopped, "wait", lambda delay: stopped.set())
                controller.run_held_successor(settings, accepted, control, stopped)
                from coire_core.models.upgrade_recovery import (
                    UpgradeExpiredRecoverySuccessorControllerStatus,
                )

                snapshot = run.read(
                    f"successor.{proof.succession_id}.status.json",
                    UpgradeExpiredRecoverySuccessorControllerStatus,
                )
                assert snapshot.owner == claim and snapshot.ready is False
                status_path = run.path(f"successor.{proof.succession_id}.status.json")
                status_before = status_path.read_bytes()
                with monkeypatch.context() as clock_test:
                    tick = [0.0]
                    budget_metric = Mock()
                    clock_test.setattr(controller, "observations", budget_metric)
                    clock_test.setattr(
                        controller,
                        "time",
                        SimpleNamespace(monotonic=lambda: tick[0]),
                        raising=False,
                    )
                    real_boundary = controller.verify_controller_boundary

                    def elapsed_boundary(*args):
                        real_boundary(*args)
                        tick[0] = 1000.0

                    clock_test.setattr(controller, "verify_controller_boundary", elapsed_boundary)
                    bounded_stop = threading.Event()
                    clock_test.setattr(bounded_stop, "wait", lambda delay: bounded_stop.set())
                    with pytest.raises(UpgradeUnavailable):
                        controller.run_held_successor(settings, accepted, control, bounded_stop)
                    assert status_path.read_bytes() == status_before
                    budget_metric.add.assert_called_once_with(
                        1, {"outcome": "refused", "node": settings.node_name}
                    )
                cancelled = threading.Event()
                gates = [0]
                real_gate = controller.require_live_successor

                def cancel_during_gate(*args):
                    real_gate(*args)
                    gates[0] += 1
                    if gates[0] == 2:
                        cancelled.set()

                monkeypatch.setattr(controller, "require_live_successor", cancel_during_gate)
                monkeypatch.setattr(
                    controller,
                    "datetime",
                    SimpleNamespace(now=lambda zone: claim.claimed_at + timedelta(seconds=2)),
                )
                controller.run_held_successor(settings, accepted, control, cancelled)
                assert status_path.read_bytes() == status_before
                sampled_gates = gates[0]
                monkeypatch.setattr(
                    controller,
                    "datetime",
                    SimpleNamespace(
                        now=lambda zone: original.consumption.transfer.intent.deadline_at
                    ),
                )
                with pytest.raises(UpgradeUnavailable):
                    controller.run_held_successor(settings, accepted, control, threading.Event())
                assert gates[0] == sampled_gates
                assert status_path.read_bytes() == status_before
                metric = Mock()
                monkeypatch.setattr(controller, "observations", metric)

                def refused(*args):
                    raise UpgradeUnavailable("authority refused")

                monkeypatch.setattr(controller, "require_live_successor", refused)
                with pytest.raises(UpgradeUnavailable):
                    controller.verify_controller_boundary(settings, claim, control)
                metric.add.assert_called_once_with(
                    1, {"outcome": "refused", "node": settings.node_name}
                )
                assert status_path.read_bytes() == status_before
        control.accept_recovery_successor.assert_called_once_with(claim)
        with pytest.raises(UpgradeUnavailable):
            require_recovery_lease(bootstrap, claim.sha256())
    assert run.path("owner.claim.json").read_bytes() == before
