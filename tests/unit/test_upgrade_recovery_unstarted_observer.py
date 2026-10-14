"""Real kernel exclusion/retired CPU actor; native runtime facts are fixtures."""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psutil
import pytest
from test_upgrade_recovery_cold_source import source_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoverySupportProcess,
    UpgradeExpiredRecoveryUnstartedObserverInput,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "alive", "owner", "core", "lost_publication"])
def test_unstarted_observer_retains_original_records_and_only_distinct_metadata(
    tmp_path, monkeypatch, fault
):
    from coire_node import expired_recovery_unstarted_observer as worker

    _, settings, owner, bootstrap, run, _, current, _ = source_fixture(
        tmp_path, monkeypatch, supervision="fixture"
    )
    PrivateUpgradeStore(bootstrap.root.parent / "fixture-records")
    independent_helper = owner.runtime.model_copy(
        update={"environment_id": "independent-helper", "environment_sha256": "f" * 64}
    )
    bootstrap.write("runtime.json", independent_helper)
    if fault != "owner":
        run.path("cold.claim.json").unlink()
        run.path("cold.activation.json").unlink()
    child = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time;time.sleep(60)"])
    try:
        identity = NodeAgentProcessIdentity(
            pid=child.pid,
            create_time_s=psutil.Process(child.pid).create_time(),
            environment_sha256=owner.runtime.environment_sha256,
        )
        run.write(
            "support.launch.receipt.json",
            UpgradeExpiredRecoverySupportProcess(
                intent=owner.intent,
                runtime=owner.runtime,
                process=identity,
                observed_at=datetime.now(UTC),
            ),
            replace=False,
        )
        if fault != "alive":
            child.terminate()
            child.wait(timeout=5)
        now = owner.intent.deadline_at + timedelta(seconds=1)
        python = tmp_path / "graphs/envs/target/bin/python3"
        monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda _: now))
        monkeypatch.setattr(
            worker,
            "sys",
            SimpleNamespace(
                platform="darwin", prefix=str(python.parent.parent), executable=str(python)
            ),
        )
        monkeypatch.setattr(
            worker,
            "platform",
            SimpleNamespace(machine=lambda: "arm64", node=lambda: "coire-edge-a"),
        )
        verified = []

        def verify_runtime(envs, runtimes, runtime):
            verified.append(runtime)
            return python

        monkeypatch.setattr(worker, "verify_prepared_runtime", verify_runtime)
        monkeypatch.setattr(worker, "_owned_native_process", lambda *_: object())
        settings = settings.model_copy(
            update={
                "training_dataset_dir": str(tmp_path / "data/datasets"),
            }
        )
        records = [bootstrap.path(k) for k in ("claim.json", "runtime.json", "status.json")]
        records += [
            tmp_path / "state/upgrades/node-process.json",
            tmp_path / "state/upgrades/maintenance.json",
            bootstrap.root / "operations" / owner.original_claim.job_id / "guard/intent.json",
        ]
        before = {p: p.read_bytes() for p in records}
        pointer = current.resolve()
        body = UpgradeExpiredRecoveryUnstartedObserverInput(
            intent=owner.intent,
            observer_release=owner.intent.support_release,
            observer_runtime=owner.runtime,
        )
        calls = []

        def read(path, model, deadline_at, **kw):
            calls.append(path)
            assert kw["core"] and kw["method"] == "POST"
            assert deadline_at == now + timedelta(seconds=15)
            assert owner.intent.deadline_at < deadline_at  # I/O only, no effect window.
            if fault == "core":
                raise UpgradeUnavailable("Core metadata scope refused")
            if path.endswith("recovery-unstarted-context"):
                return owner.intent
            assert path.endswith("recovery-unstarted-observation")
            with pytest.raises(UpgradeConflict), bootstrap.ownership("lifetime.lock"):
                pass
            with pytest.raises(UpgradeConflict), bootstrap.ownership("recovery-transfer.lock"):
                pass
            if fault == "lost_publication":
                raise UpgradeUnavailable("lost metadata publication response")
            return kw["payload"]

        control = SimpleNamespace(_read=read)
        if fault:
            message = {
                "alive": "remains present",
                "owner": "ownership/effect preimage",
                "core": "Core metadata scope refused",
                "lost_publication": "lost metadata publication",
            }[fault]
            with pytest.raises((UpgradeConflict, UpgradeUnavailable), match=message):
                worker.observe_unstarted(settings, body, control)
        else:
            proof = worker.observe_unstarted(settings, body, control)
            assert proof.startup.process == identity and proof.observer.pid == os.getpid()
            assert proof.original_maintenance_retained
            assert proof.bootstrap_runtime == independent_helper and independent_helper in verified
            assert run.path(f"unstarted.observation.{proof.sha256()}.json").exists()
        assert {p: p.read_bytes() for p in records} == before
        assert current.resolve() == pointer
        assert not run.path("cold.source.intent.json").exists()
        if fault == "alive":
            assert child.poll() is None
        if fault in {"core", "alive", "owner"}:
            assert not list(run.root.glob("unstarted.observation.*.json"))
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
