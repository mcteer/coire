"""First-install interruption cannot make the independent guard appear ready."""

import plistlib
from pathlib import Path
from unittest.mock import Mock

import pytest
from upgrade_native_fixtures import native_plan

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import NativeRuntimeSelection, UpgradeReleaseRegister
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "interrupted", "foreign_runtime", "foreign_pointer"])
def test_bootstrap_preserves_selected_source_and_reconciles_only_matching_partial_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from coire_node import upgrade_bootstrap

    plan, source, candidate = native_plan(tmp_path)
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    settings = Settings(
        node_name="coire-edge-a",
        upgrade_environment_root=str(tmp_path / "envs"),
        upgrade_bootstrap_root=str(root.root),
    )
    request = UpgradeReleaseRegister(
        prepared_artifact_id=plan.source.prepared_artifact_id,
        expected_sha256=plan.source.manifest_sha256,
    )
    artifact = Mock(return_value=plan.source)
    monkeypatch.setattr(upgrade_bootstrap, "verify_prepared_release", artifact)
    if fault in {"interrupted", "foreign_runtime"}:
        root.write("runtime.json", candidate if fault == "foreign_runtime" else source.runtime)
    if fault == "foreign_pointer":
        (tmp_path / "envs/current").unlink()
        (tmp_path / "envs/current").symlink_to(tmp_path / "envs/target")
    before = (tmp_path / "envs/current").resolve()
    try:
        if fault in {"foreign_runtime", "foreign_pointer"}:
            with pytest.raises(UpgradeConflict):
                upgrade_bootstrap.prepare_guard_bootstrap(settings, request)
            assert root.read("selection.json", NativeRuntimeSelection) is None
        else:
            selected = upgrade_bootstrap.prepare_guard_bootstrap(settings, request)
            assert selected.runtime == source.runtime
            assert selected.release_sha256 == plan.source.manifest_sha256
            assert selected.generation == 0 and selected.job_id is None
            assert upgrade_bootstrap.prepare_guard_bootstrap(settings, request) == selected
            assert root.read("selection.json", NativeRuntimeSelection) == selected
            assert not root.path("status.json").exists()
            with root.ownership("lifetime.lock"), pytest.raises(UpgradeConflict):
                upgrade_bootstrap.prepare_guard_bootstrap(settings, request)
        assert (tmp_path / "envs/current").resolve() == before
        artifact.assert_called()
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


def test_bootstrap_core_refusal_has_no_filesystem_or_artifact_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_node import upgrade_bootstrap

    artifact = Mock()
    monkeypatch.setattr(upgrade_bootstrap, "verify_prepared_release", artifact)
    settings = Settings(node_name="coire-core", upgrade_bootstrap_root=str(tmp_path / "absent"))
    with pytest.raises(UpgradeUnavailable):
        upgrade_bootstrap.prepare_guard_bootstrap(
            settings,
            UpgradeReleaseRegister(prepared_artifact_id="source", expected_sha256="a" * 64),
        )
    artifact.assert_not_called()
    assert not (tmp_path / "absent").exists()


def test_managed_node_render_preserves_command_and_enables_startup_identity(
    tmp_path: Path,
) -> None:
    from coire_node import upgrade_bootstrap

    _, source, _ = native_plan(tmp_path)
    root = PrivateUpgradeStore(tmp_path / "upgrade-bootstrap")
    root.write("runtime.json", source.runtime)
    settings = Settings(
        node_name="coire-edge-a",
        upgrade_environment_root=str(tmp_path / "envs"),
        upgrade_bootstrap_root=str(root.root),
        upgrade_prepared_dir=str(tmp_path / "upgrades/prepared"),
    )
    installed = {
        "Label": "com.coire.node",
        "UserName": "coire",
        "ProgramArguments": [str(tmp_path / "envs/current/bin/python3"), "-m", "coire_node"],
        "AbandonProcessGroup": True,
        "EnvironmentVariables": {"NODE_NAME": "coire-edge-a", "RUN_AGENT_IMAGE": "retained"},
    }
    try:
        result = plistlib.loads(
            upgrade_bootstrap.render_managed_node_plist(
                settings, plistlib.dumps(installed), "coire"
            )
        )
        assert result["ProgramArguments"] == installed["ProgramArguments"]
        assert result["EnvironmentVariables"]["UPGRADES_ENABLED"] == "1"
        assert result["EnvironmentVariables"]["RUN_AGENT_IMAGE"] == "retained"
        installed["ProgramArguments"] = ["/bin/sh", "-c", "unowned"]
        with pytest.raises(UpgradeConflict):
            upgrade_bootstrap.render_managed_node_plist(
                settings, plistlib.dumps(installed), "coire"
            )
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


def test_launchd_render_keeps_bootstrap_interpreter_after_current_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_node import upgrade_bootstrap

    _, source, _ = native_plan(tmp_path)
    root = PrivateUpgradeStore(tmp_path / "upgrade-bootstrap")
    root.write("runtime.json", source.runtime)
    settings = Settings(
        node_name="coire-edge-a",
        upgrade_environment_root=str(tmp_path / "envs"),
        upgrade_bootstrap_root=str(root.root),
    )
    (tmp_path / "envs/current").unlink()
    (tmp_path / "envs/current").symlink_to(tmp_path / "envs/target")
    template = Path("deploy/launchd/com.coire.upgrade-guard.plist.template").read_bytes()
    try:
        rendered = upgrade_bootstrap.render_guard_plist(settings, template, "coire")
        data = plistlib.loads(rendered)
        assert data["ProgramArguments"][0] == str(tmp_path / "envs/source/bin/python3")
        assert data["ProgramArguments"][1:] == ["-I", "-B", "-m", "coire_node.upgrade_guard"]
        assert data["AbandonProcessGroup"] is True
        environment = data["EnvironmentVariables"]
        assert not any("TOKEN" in name or "SECRET" in name for name in environment)
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        configured = Settings()
        assert configured.upgrades_enabled
        assert configured.node_name == "coire-edge-a"
        assert configured.upgrade_bootstrap_root == settings.upgrade_bootstrap_root
        assert configured.upgrade_environment_root == settings.upgrade_environment_root
        assert configured.node_state_dir == str(tmp_path / "state")
        with pytest.raises(UpgradeUnavailable):
            upgrade_bootstrap.render_guard_plist(settings, template, "root;command")
        with pytest.raises(UpgradeConflict):
            upgrade_bootstrap.render_guard_plist(
                settings, template.replace(b"coire_node.upgrade_guard", b"other.command"), "coire"
            )
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "active",
        "live_guard",
        "self_guard",
        "foreign_self",
        "foreign_node",
        "cleanup",
        "write",
        "after_write",
        "replaced_node",
    ],
)
def test_idle_helper_refresh_retains_history_and_replays_interrupted_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from coire_core.models.upgrades import (
        NativeBootstrapRefresh,
        NativeGuardStatus,
        NativeNodeProcess,
        NativeUpgradeClaim,
        NodeAgentProcessIdentity,
        PreparedNativeRuntime,
        UpgradeEvidence,
        UpgradeReceipt,
    )
    from coire_node import upgrade_bootstrap, upgrade_control, upgrade_guard

    plan, source, target = native_plan(tmp_path)
    selected = source.model_copy(
        update={
            "runtime": target,
            "release_sha256": plan.target.manifest_sha256,
            "job_id": plan.operation.job_id,
            "generation": 1,
        }
    )
    (tmp_path / "envs/current").unlink()
    (tmp_path / "envs/current").symlink_to(tmp_path / "envs/target")
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    root.write("runtime.json", source.runtime)
    root.write("selection.json", selected)
    claim = NativeUpgradeClaim(
        node="coire-edge-a",
        job_id=plan.operation.job_id,
        generation=1,
        plan_sha256=plan.authority_sha256(),
        state="active" if fault == "active" else "released",
        updated_at=datetime.now(UTC),
    )
    root.write("claim.json", claim)
    guard = NodeAgentProcessIdentity(
        pid=123455, create_time_s=100.0, environment_sha256=source.runtime.environment_sha256
    )
    root.write(
        "status.json",
        NativeGuardStatus(
            node="coire-edge-a",
            bootstrap_runtime=source.runtime,
            process=guard,
            selected=selected,
            ready=True,
            observed_at=datetime.now(UTC),
        ),
    )
    node = NodeAgentProcessIdentity(
        pid=123456,
        create_time_s=101.0,
        environment_sha256=(
            source.runtime.environment_sha256
            if fault == "foreign_node"
            else target.environment_sha256
        ),
    )
    state = PrivateUpgradeStore(tmp_path / "state/upgrades")
    state.write(
        "node-process.json",
        NativeNodeProcess(
            selected=source if fault == "foreign_node" else selected,
            process=node,
            recorded_at=datetime.now(UTC),
        ),
    )
    history = root.root / "operations" / plan.operation.job_id
    history.mkdir(parents=True)
    preserved = history / "history.marker"
    preserved.write_bytes(b"never rewrite completed operation history")
    receipt = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=1,
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
        Mock(return_value=SimpleNamespace(receipts=SimpleNamespace(latest=lambda: receipt))),
    )
    monkeypatch.setattr(upgrade_guard, "_owned_node_process", Mock(return_value=Mock()))
    monkeypatch.setattr(
        upgrade_guard,
        "_owned_native_process",
        Mock(
            return_value=(
                Mock(create_time=Mock(return_value=guard.create_time_s))
                if fault in {"live_guard", "self_guard", "foreign_self"}
                else None
            )
        ),
    )
    monkeypatch.setattr(
        upgrade_bootstrap, "verify_prepared_release", Mock(return_value=plan.target)
    )
    settings = Settings(
        node_name="coire-edge-a",
        node_state_dir=str(tmp_path / "state"),
        upgrade_environment_root=str(tmp_path / "envs"),
        upgrade_bootstrap_root=str(root.root),
    )
    request = UpgradeReleaseRegister(
        prepared_artifact_id=plan.target.prepared_artifact_id,
        expected_sha256=plan.target.manifest_sha256,
    )
    original_write = PrivateUpgradeStore.write
    interrupted = False

    def write(self: PrivateUpgradeStore, key: str, value: object, *, replace: bool = True) -> None:
        nonlocal interrupted
        if (
            fault in {"write", "replaced_node"}
            and not interrupted
            and self.root == root.root
            and key == "runtime.json"
        ):
            interrupted = True
            raise UpgradeUnavailable("injected owner publication interruption")
        original_write(self, key, value, replace=replace)
        if (
            fault == "after_write"
            and not interrupted
            and self.root == root.root
            and key == "runtime.json"
        ):
            interrupted = True
            raise UpgradeUnavailable("injected interruption after owner publication")

    monkeypatch.setattr(PrivateUpgradeStore, "write", write)
    try:
        if fault in {"self_guard", "foreign_self"}:
            monkeypatch.setattr(upgrade_bootstrap.os, "getpid", lambda: guard.pid)
            identity = (
                guard if fault == "self_guard" else guard.model_copy(update={"pid": guard.pid + 1})
            )
            with root.ownership("control.lock"), root.ownership("lifetime.lock"):
                if fault == "foreign_self":
                    with pytest.raises(UpgradeConflict):
                        upgrade_bootstrap.refresh_guard_source(
                            settings,
                            root,
                            target,
                            selected.release_sha256,
                            claim,
                            refreshing_guard=identity,
                        )
                    assert root.read("runtime.json", PreparedNativeRuntime) == source.runtime
                else:
                    assert (
                        upgrade_bootstrap.refresh_guard_source(
                            settings,
                            root,
                            target,
                            selected.release_sha256,
                            claim,
                            refreshing_guard=identity,
                        )
                        == selected
                    )
                    refresh = root.read("bootstrap-refresh.json", NativeBootstrapRefresh)
                    assert refresh is not None and refresh.state == "committed"
                    assert refresh.source_process == node
            return
        if fault in {"active", "live_guard", "foreign_node", "cleanup"}:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                upgrade_bootstrap.prepare_guard_bootstrap(settings, request)
            assert root.read("runtime.json", PreparedNativeRuntime) == source.runtime
            assert not list(root.root.glob("refresh-*.json"))
        else:
            if fault in {"write", "after_write", "replaced_node"}:
                with pytest.raises(UpgradeUnavailable):
                    upgrade_bootstrap.prepare_guard_bootstrap(settings, request)
                assert root.read("runtime.json", PreparedNativeRuntime) == (
                    target if fault == "after_write" else source.runtime
                )
            if fault == "replaced_node":
                state.write(
                    "node-process.json",
                    NativeNodeProcess(
                        selected=selected,
                        process=node.model_copy(update={"create_time_s": 102.0}),
                        recorded_at=datetime.now(UTC),
                    ),
                )
                with pytest.raises(UpgradeConflict):
                    upgrade_bootstrap.prepare_guard_bootstrap(settings, request)
                assert root.read("runtime.json", PreparedNativeRuntime) == source.runtime
                return
            assert upgrade_bootstrap.prepare_guard_bootstrap(settings, request) == selected
            assert upgrade_bootstrap.prepare_guard_bootstrap(settings, request) == selected
            refresh = root.read(
                f"refresh-1-{target.environment_sha256}.json", NativeBootstrapRefresh
            )
            assert refresh is not None and refresh.state == "committed"
            assert refresh.previous_runtime == source.runtime and refresh.selected == selected
            assert root.read("runtime.json", PreparedNativeRuntime) == target
        assert root.read("claim.json", NativeUpgradeClaim) == claim
        assert root.read("selection.json", NativeRuntimeSelection) == selected
        assert preserved.read_bytes() == b"never rewrite completed operation history"
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
