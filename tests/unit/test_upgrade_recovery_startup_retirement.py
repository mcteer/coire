"""Real CPU launcher retirement; immutable native module identity is a fixture."""

import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySupportLaunchIntent,
    UpgradeExpiredRecoverySupportProcess,
    UpgradeExpiredRecoverySupportStartup,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "revoked", "expired", "stopped"])
def test_startup_waits_for_actual_launcher_retirement_without_native_effects(
    tmp_path, monkeypatch, fault
):
    from coire_node import expired_recovery_startup_wait as worker

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    intent = owner.consumption.transfer.intent
    intent = intent.model_copy(
        update={"request": intent.request.model_copy(update={"recovery_mode": "cold"})}
    )
    startup = UpgradeExpiredRecoverySupportStartup(
        intent=intent, runtime=owner.runtime, prepared_at=datetime.now(UTC)
    )
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    run = PrivateUpgradeStore(root.path("recovery"))
    actor = NodeAgentProcessIdentity(
        pid=os.getpid(),
        create_time_s=psutil.Process().create_time(),
        environment_sha256=owner.runtime.environment_sha256,
    )
    script = tmp_path / "launcher.py"
    script.write_text("import time; time.sleep(60)")
    child = subprocess.Popen([sys.executable, "-I", "-B", str(script)])
    stopped = threading.Event()
    reached = threading.Event()
    control = SimpleNamespace(
        verify_support_context=Mock(), read_cold_recovery_continuation=Mock(return_value=None)
    )

    def fresh_context(value):
        reached.set()
        if fault == "revoked":
            raise UpgradeUnavailable("revoked")
        return value

    control.verify_support_context.side_effect = fresh_context
    monkeypatch.setattr(worker, "verify_passive_support_identity", lambda *_: actor)
    settings = Settings(upgrade_environment_root=str(tmp_path / "envs"))
    try:
        launch = UpgradeExpiredRecoverySupportLaunchIntent(
            startup=startup,
            launcher=NodeAgentProcessIdentity(
                pid=child.pid,
                create_time_s=psutil.Process(child.pid).create_time(),
                environment_sha256=owner.runtime.environment_sha256,
            ),
            started_at=datetime.now(UTC),
        )
        run.write("support.launch.intent.json", launch, replace=False)
        run.write(
            "support.launch.receipt.json",
            UpgradeExpiredRecoverySupportProcess(
                intent=intent, runtime=owner.runtime, process=actor, observed_at=datetime.now(UTC)
            ),
            replace=False,
        )
        if fault == "expired":
            monkeypatch.setattr(
                worker,
                "datetime",
                SimpleNamespace(now=lambda _: launch.started_at + timedelta(seconds=31)),
            )
            with pytest.raises(UpgradeUnavailable, match="captured launcher"):
                worker.wait_for_launcher_retirement(settings, startup, run, control, stopped)
            assert child.poll() is None
        elif fault == "stopped":
            stopped.set()
            assert not worker.wait_for_launcher_retirement(settings, startup, run, control, stopped)
            assert child.poll() is None
        elif fault == "revoked":
            with pytest.raises(UpgradeUnavailable, match="revoked"):
                worker.wait_for_launcher_retirement(settings, startup, run, control, stopped)
            assert child.poll() is None
        else:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    worker.wait_for_launcher_retirement, settings, startup, run, control, stopped
                )
                assert reached.wait(5)
                assert not future.done()
                child.terminate()
                child.wait(timeout=5)
                assert future.result(timeout=5)
        assert not root.path("claim.json").exists()
        assert not root.path("runtime.json").exists()
    finally:
        stopped.set()
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


@pytest.mark.parametrize("fault", [None, "foreign_guard", "missing_writer"])
def test_late_startup_allows_only_the_recorded_live_source_writer(tmp_path, monkeypatch, fault):
    from test_upgrade_recovery_cold_retained_source import continuation_fixture

    from coire_node import expired_recovery_startup_wait as worker
    from coire_node import upgrade_guard

    _, _, _, settings, owner, bootstrap, run, _, source, control = continuation_fixture(
        tmp_path, monkeypatch, post_handback=True
    )
    now = owner.intent.accepted_at
    actor = owner.process
    startup = UpgradeExpiredRecoverySupportStartup(
        intent=owner.intent, runtime=owner.runtime, prepared_at=now
    )
    launch = UpgradeExpiredRecoverySupportLaunchIntent(
        startup=startup,
        launcher=actor.model_copy(update={"pid": actor.pid + 1}),
        started_at=now,
    )
    run.write("support.launch.intent.json", launch)
    run.write(
        "support.launch.receipt.json",
        UpgradeExpiredRecoverySupportProcess(
            intent=owner.intent,
            runtime=owner.runtime,
            process=actor,
            observed_at=now,
        ),
    )
    keeper = owner.continuation.post_handback.source_helper
    bootstrap.write("status.json", keeper.model_copy(update={"observed_at": now}))
    python = tmp_path / "graphs/envs/source/bin/python3"
    monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(worker, "verify_passive_support_identity", lambda *_: actor)
    monkeypatch.setattr(
        worker.psutil, "Process", Mock(side_effect=psutil.NoSuchProcess(launch.launcher.pid))
    )
    monkeypatch.setattr("coire_node.upgrade_runtime.verify_prepared_runtime", lambda *_: python)
    monkeypatch.setattr(
        upgrade_guard,
        "_owned_node_process",
        lambda *_: SimpleNamespace(cmdline=lambda: [str(python), "-I", "-B", "-m", "coire_node"]),
    )
    monkeypatch.setattr(
        upgrade_guard,
        "_owned_native_process",
        lambda *_: SimpleNamespace(
            cmdline=lambda: (
                ["foreign"]
                if fault == "foreign_guard"
                else [str(python), "-I", "-B", "-m", "coire_node.upgrade_guard"]
            )
        ),
    )
    monkeypatch.setattr(
        worker,
        "native_process_entries",
        lambda *_: tuple(
            SimpleNamespace(process_id=identity.pid, process_created_at=identity.create_time_s)
            for identity in (actor, source.process, keeper.process)
        ),
    )
    from contextlib import nullcontext

    with bootstrap.ownership("lifetime.lock") if fault != "missing_writer" else nullcontext():
        if fault is not None:
            with pytest.raises(UpgradeUnavailable, match="Source writer"):
                worker.wait_for_launcher_retirement(
                    settings, startup, run, control, threading.Event()
                )
        else:
            assert worker.wait_for_launcher_retirement(
                settings, startup, run, control, threading.Event()
            )


@pytest.mark.parametrize("fault", [None, "live_keeper", "writer"])
def test_retired_keeper_startup_proves_absence_without_requiring_stale_readiness(
    tmp_path, monkeypatch, fault
):
    from test_upgrade_partial_handback_observer import (
        test_partial_ready_handback_is_metadata_only_and_keeps_original_deadlines,
    )

    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryIntent
    from coire_core.models.upgrade_recovery_continuation import UpgradeColdContinuationGrant
    from coire_core.models.upgrades import UpgradeMaintenance
    from coire_node import expired_recovery_startup_wait as worker
    from coire_node import recovery_identity, upgrade_guard

    captured = []
    test_partial_ready_handback_is_metadata_only_and_keeps_original_deadlines(
        tmp_path,
        monkeypatch,
        "keeper_retired",
        capture_success=lambda *values: captured.append(values),
    )
    settings, old_owner, bootstrap, _old_run, source, proof, _context, _body, _, control = captured[
        0
    ]
    now = proof.observed_at + timedelta(seconds=1)
    child_id = "01ARZ3NDEKTSV4RRFFQ69G5FAZ"
    fence = UpgradeMaintenance(
        job_id=child_id,
        generation=old_owner.original_claim.generation + 2,
        targets=(settings.node_name,),
        state="active",
        updated_at=now,
        drain_deadline_at=now + timedelta(seconds=300),
        initiator_user_id=old_owner.intent.accepted_by,
    )
    intent = UpgradeExpiredRecoveryIntent.model_validate(
        old_owner.intent.model_dump(mode="json")
        | {
            "recovery_id": child_id,
            "accepted_at": now,
            "deadline_at": fence.drain_deadline_at,
            "local_recovery_maintenance": fence.model_dump(mode="json"),
        }
    )
    grant = UpgradeColdContinuationGrant(
        grant_id="01ARZ3NDEKTSV4RRFFQ69G5FB0",
        request_sha256="c" * 64,
        intent=intent,
        binding=proof.binding,
        unstarted_startup=proof.unstarted_startup,
        issued_by=intent.accepted_by,
        issued_at=now,
    )
    run = PrivateUpgradeStore(bootstrap.root / "expired-recovery" / child_id)
    actor = NodeAgentProcessIdentity(
        pid=555006,
        create_time_s=now.timestamp(),
        environment_sha256=old_owner.runtime.environment_sha256,
    )
    startup = UpgradeExpiredRecoverySupportStartup(
        intent=intent, runtime=old_owner.runtime, prepared_at=now
    )
    launcher = actor.model_copy(update={"pid": 555007})
    run.write(
        "support.launch.intent.json",
        UpgradeExpiredRecoverySupportLaunchIntent(
            startup=startup, launcher=launcher, started_at=now
        ),
    )
    run.write(
        "support.launch.receipt.json",
        UpgradeExpiredRecoverySupportProcess(
            intent=intent, runtime=old_owner.runtime, process=actor, observed_at=now
        ),
    )
    python = tmp_path / "graphs/envs/source/bin/python3"
    control.verify_support_context = Mock(return_value=intent)
    control.read_cold_recovery_continuation = Mock(return_value=grant)
    monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(worker, "verify_passive_support_identity", lambda *_: actor)
    monkeypatch.setattr(
        worker.psutil, "Process", Mock(side_effect=psutil.NoSuchProcess(launcher.pid))
    )
    monkeypatch.setattr(recovery_identity, "verify_retired_identity", lambda *_: None)
    monkeypatch.setattr("coire_node.upgrade_runtime.verify_prepared_runtime", lambda *_: python)
    monkeypatch.setattr(
        upgrade_guard,
        "_owned_node_process",
        lambda *_: SimpleNamespace(cmdline=lambda: [str(python), "-I", "-B", "-m", "coire_node"]),
    )
    monkeypatch.setattr(
        upgrade_guard,
        "_owned_native_process",
        lambda *_: object() if fault == "live_keeper" else None,
    )
    monkeypatch.setattr(
        worker,
        "native_process_entries",
        lambda *_: tuple(
            SimpleNamespace(process_id=p.pid, process_created_at=p.create_time_s)
            for p in (actor, source.process)
        ),
    )
    from contextlib import nullcontext

    with bootstrap.ownership("lifetime.lock") if fault == "writer" else nullcontext():
        if fault:
            with pytest.raises(UpgradeUnavailable, match="retired Source keeper"):
                worker.wait_for_launcher_retirement(
                    settings, startup, run, control, threading.Event()
                )
        else:
            assert worker.wait_for_launcher_retirement(
                settings, startup, run, control, threading.Event()
            )
    assert not run.path("cold.claim.json").exists()
    if fault is None:
        from coire_core.models.upgrades import UpgradeMaintenance
        from coire_node import expired_recovery_cold_owner as physical
        from coire_node import expired_recovery_source_keeper_retirement as retirement_worker
        from coire_node.recovery_lease import require_recovery_lease

        node_store = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades")
        old_fence = node_store.read("maintenance.json", UpgradeMaintenance)
        assert old_fence.state == "fencing"
        for module in (physical, retirement_worker):
            monkeypatch.setattr(module, "verify_passive_support_identity", lambda *_: actor)
            monkeypatch.setattr(module, "verify_retired_identity", lambda *_: None)
            monkeypatch.setattr(module, "verify_prepared_runtime", lambda *_: python)
            monkeypatch.setattr(
                module,
                "_owned_node_process",
                lambda *_: SimpleNamespace(
                    cmdline=lambda: [str(python), "-I", "-B", "-m", "coire_node"]
                ),
            )
        monkeypatch.setattr(retirement_worker, "_owned_native_process", lambda *_: None)
        monkeypatch.setattr(retirement_worker, "datetime", SimpleNamespace(now=lambda _: now))
        monkeypatch.setattr(physical, "datetime", SimpleNamespace(now=lambda _: now))
        monkeypatch.setattr(
            physical, "recovery_kernel_boot_identity", lambda *_: old_owner.kernel_boot_identity
        )
        monkeypatch.setattr(physical, "fixed_launchctl_probe", lambda *_: lambda _: None)
        monkeypatch.setattr(
            physical, "observe_fixture_supervisor", lambda *_: old_owner.retired_supervisor
        )
        monkeypatch.setattr(physical, "native_process_entries", worker.native_process_entries)
        control.accept_cold_recovery_owner = Mock(side_effect=lambda value: value)
        control.verify_cold_recovery_owner = Mock(side_effect=lambda value: value)
        with physical.initial_cold_owner(settings, intent, old_owner.runtime, control) as claimed:
            assert (
                node_store.read("maintenance.json", UpgradeMaintenance)
                == intent.local_recovery_maintenance
            )
            assert run.path("local.recovery.fence.receipt.json").exists()
            assert run.path("cold.activation.json").exists()
            require_recovery_lease(bootstrap, claimed.sha256())
        assert source.process == claimed.last_node.process
