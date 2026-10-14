"""Real CPU launcher retirement; immutable native module identity is a fixture."""

import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
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
