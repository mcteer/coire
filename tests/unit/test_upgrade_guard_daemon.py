"""Recovery heartbeat stays independent of a blocked execution worker."""

import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from upgrade_native_fixtures import native_plan

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import NativeUpgradeClaim
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


def test_guard_keeps_exclusive_owner_and_heartbeat_during_worker_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_node import upgrade_control, upgrade_guard, upgrade_workflow

    plan, source, _ = native_plan(tmp_path)
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    root.write("runtime.json", source.runtime)
    root.write(
        "claim.json",
        NativeUpgradeClaim(
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            node="coire-edge-a",
            plan_sha256=plan.authority_sha256(),
            updated_at=datetime.now(UTC),
        ),
    )
    settings = Settings(
        upgrades_enabled=True,
        node_name="coire-edge-a",
        node_token="test-node-control",
        node_control_host="coire-edge-a.lab",
        upgrade_bootstrap_root=str(root.root),
    )
    stopped = threading.Event()
    fresh_heartbeat = threading.Event()
    published = 0

    def publish(*_args: object) -> None:
        nonlocal published
        published += 1
        if published >= 2:
            fresh_heartbeat.set()

    worker = Mock()

    def advance(*_args: object) -> None:
        try:
            with pytest.raises(UpgradeConflict), root.ownership("lifetime.lock"):
                pytest.fail("another recovery owner acquired the lifetime lease")
            assert fresh_heartbeat.wait(4.0), "blocked worker prevented independent heartbeat"
            worker()
        finally:
            stopped.set()

    monkeypatch.setattr(upgrade_guard.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(upgrade_guard, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(upgrade_guard, "publish_guard_status", publish)
    monkeypatch.setattr(
        upgrade_control, "NativeUpgradeControl", lambda _settings: SimpleNamespace(journal=Mock())
    )
    monkeypatch.setattr(upgrade_workflow, "advance_guard_step", advance)
    try:
        upgrade_guard.run_native_guard(settings, stopped)
        worker.assert_called_once()
        assert published >= 2
        with root.ownership("lifetime.lock"):
            pass
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


@pytest.mark.parametrize("fault", ["disabled", "core", "missing_token"])
def test_guard_refuses_unavailable_authority_before_creating_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from coire_node import upgrade_guard

    monkeypatch.setattr(upgrade_guard.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(upgrade_guard, "sys", SimpleNamespace(platform="darwin"))
    root = tmp_path / "absent"
    settings = Settings(
        upgrades_enabled=fault != "disabled",
        node_name="coire-core" if fault == "core" else "coire-edge-a",
        node_token="" if fault == "missing_token" else "test-node-control",
        upgrade_bootstrap_root=str(root),
    )
    with pytest.raises(UpgradeUnavailable):
        upgrade_guard.run_native_guard(settings, threading.Event())
    assert not root.exists()


@pytest.mark.parametrize(
    "fault", [None, "missing", "intent", "cleanup", "foreign_launcher", "active_source"]
)
def test_retained_launcher_delegates_only_from_committed_source_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from coire_core.models.upgrades import (
        NativeBootstrapRefresh,
        NodeAgentProcessIdentity,
        UpgradeEvidence,
        UpgradeReceipt,
    )
    from coire_node import upgrade_control, upgrade_guard

    plan, source, target = native_plan(tmp_path)
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    root.write("launcher-runtime.json", source.runtime)
    root.write("runtime.json", target)
    selected = source.model_copy(
        update={
            "runtime": target,
            "release_sha256": plan.target.manifest_sha256,
            "job_id": plan.operation.job_id,
            "generation": plan.operation.generation,
        }
    )
    if fault != "missing":
        root.write(
            "bootstrap-refresh.json",
            NativeBootstrapRefresh(
                previous_runtime=source.runtime,
                selected=selected,
                source_process=NodeAgentProcessIdentity(
                    pid=123456, create_time_s=101.0, environment_sha256=target.environment_sha256
                ),
                prepared_at=datetime.now(UTC),
                state="intent" if fault == "intent" else "committed",
            ),
        )
    receipt = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        sequence=10,
        effect_id="native-candidate-closed",
        phase="succeeded",
        outcome="complete",
        occurred_at=datetime.now(UTC),
        observed_sha256=plan.target.manifest_sha256,
        evidence=UpgradeEvidence(
            runtime_sha256=target.environment_sha256,
            check="node",
            generated_tokens=10,
            output_sha256="a" * 64,
            cleanup_verified=fault != "cleanup",
        ).model_dump_json(),
    )
    monkeypatch.setattr(
        upgrade_control.NativeUpgradeControl,
        "journal",
        Mock(
            return_value=SimpleNamespace(
                plan=plan, receipts=SimpleNamespace(latest=lambda: receipt)
            )
        ),
    )
    if fault == "active_source":
        root.write(
            "claim.json",
            NativeUpgradeClaim(
                job_id=plan.operation.job_id,
                generation=plan.operation.generation,
                node="coire-edge-a",
                plan_sha256=plan.authority_sha256(),
                updated_at=datetime.now(UTC),
            ),
        )
    monkeypatch.setattr(
        upgrade_guard,
        "sys",
        SimpleNamespace(
            prefix=str(
                tmp_path / "envs" / ("foreign" if fault == "foreign_launcher" else "source")
            ),
            executable=str(tmp_path / "envs/source/bin/python3"),
        ),
    )
    if fault == "foreign_launcher":
        (tmp_path / "envs/foreign").mkdir()
    settings = Settings(
        upgrades_enabled=True,
        node_name="coire-edge-a",
        upgrade_environment_root=str(tmp_path / "envs"),
        upgrade_bootstrap_root=str(root.root),
    )
    try:
        if fault is None:
            assert upgrade_guard.retained_guard_delegate(settings) == target
            # The current pointer can remain Source or be Candidate during recovery;
            # delegation does not invent a new readiness or require a living node.
            assert (tmp_path / "envs/current").resolve() == tmp_path / "envs/source"
        else:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                upgrade_guard.retained_guard_delegate(settings)
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


def test_idle_guard_handover_releases_writer_without_signalling_node_or_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_node import upgrade_guard

    plan, source, target = native_plan(tmp_path)
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    root.write("runtime.json", source.runtime)
    root.write(
        "claim.json",
        NativeUpgradeClaim(
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            node="coire-edge-a",
            plan_sha256=plan.authority_sha256(),
            state="released",
            updated_at=datetime.now(UTC),
        ),
    )
    settings = Settings(
        upgrades_enabled=True,
        node_name="coire-edge-a",
        node_token="test-node-control",
        node_control_host="coire-edge-a.lab",
        upgrade_bootstrap_root=str(root.root),
    )
    stopped = threading.Event()
    monkeypatch.setattr(upgrade_guard.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(upgrade_guard, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(upgrade_guard, "publish_guard_status", Mock())
    monkeypatch.setattr(upgrade_guard, "refresh_running_guard", Mock(return_value=target))
    stop_node = Mock()
    monkeypatch.setattr(upgrade_guard, "stop_owned_node_process", stop_node)
    try:
        assert upgrade_guard.run_native_guard(settings, stopped) == target
        assert not stopped.is_set()
        stop_node.assert_not_called()
        assert not any(thread.name == "coire-upgrade-heartbeat" for thread in threading.enumerate())
        with root.ownership("lifetime.lock"):
            pass
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


def test_published_guard_handover_replays_after_captured_node_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_core import upgrade_artifacts
    from coire_core.models.upgrades import (
        NativeBootstrapRefresh,
        NodeAgentProcessIdentity,
        UpgradeEvidence,
        UpgradeReceipt,
    )
    from coire_node import upgrade_control, upgrade_guard

    plan, source, target = native_plan(tmp_path)
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    selected = source.model_copy(
        update={
            "runtime": target,
            "release_sha256": plan.target.manifest_sha256,
            "job_id": plan.operation.job_id,
            "generation": plan.operation.generation,
        }
    )
    root.write("selection.json", selected)
    root.write("runtime.json", target)
    root.write(
        "claim.json",
        NativeUpgradeClaim(
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            node="coire-edge-a",
            plan_sha256=plan.authority_sha256(),
            state="released",
            updated_at=datetime.now(UTC),
        ),
    )
    root.write(
        "bootstrap-refresh.json",
        NativeBootstrapRefresh(
            previous_runtime=source.runtime,
            selected=selected,
            source_process=NodeAgentProcessIdentity(
                pid=123456, create_time_s=101.0, environment_sha256=target.environment_sha256
            ),
            prepared_at=datetime.now(UTC),
            state="committed",
        ),
    )
    receipt = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        sequence=10,
        effect_id="native-candidate-closed",
        phase="succeeded",
        outcome="complete",
        occurred_at=datetime.now(UTC),
        observed_sha256=plan.target.manifest_sha256,
        evidence=UpgradeEvidence(
            runtime_sha256=target.environment_sha256,
            check="node",
            generated_tokens=10,
            output_sha256="a" * 64,
            cleanup_verified=True,
        ).model_dump_json(),
    )
    monkeypatch.setattr(
        upgrade_control.NativeUpgradeControl,
        "journal",
        Mock(
            return_value=SimpleNamespace(
                plan=plan, receipts=SimpleNamespace(latest=lambda: receipt)
            )
        ),
    )
    monkeypatch.setattr(
        upgrade_artifacts, "verify_prepared_release", Mock(return_value=plan.target)
    )
    settings = Settings(
        node_name="coire-edge-a",
        upgrade_environment_root=str(tmp_path / "envs"),
        upgrade_bootstrap_root=str(root.root),
        node_state_dir=str(tmp_path / "absent-node-state"),
    )
    try:
        assert (
            upgrade_guard.refresh_running_guard(
                settings,
                NodeAgentProcessIdentity(
                    pid=123455,
                    create_time_s=100.0,
                    environment_sha256=source.runtime.environment_sha256,
                ),
            )
            == target
        )
        assert not (tmp_path / "absent-node-state").exists()
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
