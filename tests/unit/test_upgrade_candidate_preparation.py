"""Candidate imports require a fresh, fenced, positively idle Studio source."""

import platform
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from upgrade_fixtures import plan

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import NodeUpgradeInventory, NodeUpgradeStatus
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", ["core", "unready", "stale", "missing_owners"])
def test_candidate_preparation_refuses_before_any_native_import(
    fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_node import upgrade_executor, upgrade_runtime

    operation = plan()
    now = datetime.now(UTC)
    status = NodeUpgradeStatus(
        node="coire-edge-a",
        bootstrap_ready=fault != "unready",
        release_sha256=operation.source.manifest_sha256,
        runtime_sha256=operation.source.manifest.environment_sha256,
        environment_id="source",
        observed_at=now - timedelta(seconds=10 if fault == "stale" else 0),
    )
    inventory = NodeUpgradeInventory(
        job_id=operation.operation.job_id,
        generation=1,
        node="coire-edge-a",
        runtime_sha256=operation.source.manifest.environment_sha256,
        observed_at=now,
        owners=(),
    )
    monkeypatch.setattr(upgrade_runtime, "smoke", lambda *_: pytest.fail("native imports ran"))
    with pytest.raises(UpgradeUnavailable):
        upgrade_executor.prepare_native_candidate(
            Settings(node_name="core" if fault == "core" else "coire-edge-a"),
            operation,
            status,
            inventory,
        )


@pytest.mark.parametrize("fault", [None, "changed_fence", "changed_source", "import_failure"])
def test_preparation_verifies_graphs_and_retains_disk_until_owned_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    import hashlib
    import subprocess

    from test_upgrade_runtime_selection import prepared

    from coire_core.models.upgrades import PreparedNativeRuntime, UpgradeOwnerInventory
    from coire_core.upgrade_journal import PrivateUpgradeStore
    from coire_node import upgrade_executor, upgrade_runtime
    from coire_node.maintenance import REQUIRED_OWNERS

    envs, interpreters, source = prepared(tmp_path)
    environment = envs / "target"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin/python3").symlink_to(interpreters / source.interpreter_id / "bin/python3")
    (environment / "module.py").write_bytes(b"candidate-module")
    upgrade_runtime.seal_tree(environment)
    candidate = PreparedNativeRuntime(
        environment_id="target",
        interpreter_id=source.interpreter_id,
        environment_sha256=upgrade_runtime.environment_content_sha256(
            environment, interpreters / source.interpreter_id
        ),
        interpreter_sha256=source.interpreter_sha256,
    )
    PrivateUpgradeStore(envs / ".identities").write("target.json", candidate)
    value = plan()
    PrivateUpgradeStore(tmp_path / "prepared")
    releases = []
    for release, runtime in ((value.source, source), (value.target, candidate)):
        content = b"wheel-fixture"
        manifest = release.manifest.model_copy(
            update={
                "environment_sha256": runtime.environment_sha256,
                "artifact_sha256s": {"wheel": hashlib.sha256(content).hexdigest()},
                "staged_bytes": len(content),
            }
        )
        release = release.model_copy(
            update={"manifest": manifest, "manifest_sha256": manifest.sha256()}
        )
        catalog = PrivateUpgradeStore(tmp_path / "prepared" / release.prepared_artifact_id)
        catalog.path("wheel").write_bytes(content)
        catalog.path("wheel").chmod(0o600)
        catalog.write("release.json", release)
        releases.append(release)
    value = value.model_copy(
        update={
            "source": releases[0],
            "target": releases[1],
            "operation": value.operation.model_copy(
                update={
                    "source_sha256": releases[0].manifest_sha256,
                    "target_sha256": releases[1].manifest_sha256,
                }
            ),
        }
    )
    settings = Settings(
        node_name="coire-edge-a",
        node_state_dir=str(tmp_path / "state"),
        upgrade_environment_root=str(envs),
        upgrade_prepared_dir=str(tmp_path / "prepared"),
        upgrade_disk_margin_bytes=32 * 1024,
    )
    fence_store = PrivateUpgradeStore(tmp_path / "state/upgrades")
    fence_store.write("maintenance.json", value.maintenance)
    status = NodeUpgradeStatus(
        node="coire-edge-a",
        bootstrap_ready=True,
        release_sha256=value.source.manifest_sha256,
        runtime_sha256=source.environment_sha256,
        environment_id=source.environment_id,
        observed_at=datetime.now(UTC),
    )
    inventory = NodeUpgradeInventory(
        job_id=value.operation.job_id,
        generation=1,
        node="coire-edge-a",
        runtime_sha256=source.environment_sha256,
        observed_at=datetime.now(UTC),
        owners=tuple(
            UpgradeOwnerInventory(owner=name, status="complete", entries=())
            for name in REQUIRED_OWNERS
        ),
    )
    calls = []

    def smoke(python: Path) -> None:
        calls.append(python)
        if fault == "changed_fence":
            fence_store.write(
                "maintenance.json", value.maintenance.model_copy(update={"state": "released"})
            )
        elif fault == "changed_source":
            path = envs / "source/module.py"
            path.chmod(0o600)
            path.write_bytes(b"changed")
            path.chmod(0o400)
        elif fault == "import_failure":
            raise subprocess.CalledProcessError(1, ["candidate-python"])

    monkeypatch.setattr(upgrade_runtime, "smoke", smoke)
    monkeypatch.setattr(upgrade_executor.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(upgrade_executor, "is_darwin", lambda _: True)
    try:
        if fault is None:
            assert (
                upgrade_executor.prepare_native_candidate(settings, value, status, inventory)
                == candidate
            )
        else:
            with pytest.raises(UpgradeUnavailable):
                upgrade_executor.prepare_native_candidate(settings, value, status, inventory)
        assert calls == [envs / "target/bin/python3"]
        assert (envs / ".upgrade-reservations").stat().st_mode & 0o077 == 0
        ledger = upgrade_executor.NativeDiskReservation(
            envs / ".upgrade-reservations" / value.operation.job_id, value
        )
        assert ledger.current().state == "held"
        assert not (envs / "current").exists()
        ledger.release()
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


@pytest.mark.parametrize(
    ("system", "machine", "host"),
    [
        ("Darwin", "arm64", "coire-core"),
        ("Linux", "arm64", "coire-edge-a"),
        ("Darwin", "x86_64", "coire-edge-a"),
    ],
)
def test_direct_native_smoke_refuses_nonstudio_before_subprocess(
    monkeypatch, system, machine, host
):
    from unittest.mock import Mock

    from coire_node import upgrade_runtime

    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(platform, "node", lambda: host)
    run = Mock(side_effect=AssertionError("non-Studio reached native import"))
    monkeypatch.setattr(upgrade_runtime.subprocess, "run", run)
    with pytest.raises(ValueError, match="registered Apple Silicon Studio"):
        upgrade_runtime.smoke(Path("/retained/env/bin/python3"))
    run.assert_not_called()


def test_recovery_import_failure_stops_remaining_native_smoke_commands(monkeypatch):
    import subprocess
    from unittest.mock import Mock

    from coire_node import upgrade_runtime

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(platform, "node", lambda: "coire-edge-a")
    run = Mock(side_effect=subprocess.CalledProcessError(1, "isolated import"))
    monkeypatch.setattr(upgrade_runtime.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        upgrade_runtime.smoke(Path("/retained/env/bin/python3"))
    assert run.call_count == 1
    assert "coire_node.expired_recovery_support" in run.call_args.args[0][-1]


def test_import_checks_cannot_use_checkout_or_user_python_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from coire_node import upgrade_runtime

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(platform, "node", lambda: "coire-edge-a")
    monkeypatch.setenv("PYTHONPATH", "/foreign/checkout")
    monkeypatch.setenv("PYTHONHOME", "/foreign/interpreter")
    run = Mock()
    monkeypatch.setattr(upgrade_runtime.subprocess, "run", run)
    upgrade_runtime.smoke(Path("/retained/env/bin/python3"))
    assert run.call_count == 5
    import_command = run.call_args_list[0].args[0][-1]
    for module in (
        "coire_node.upgrade_guard",
        "coire_node.expired_recovery_support",
        "coire_node.expired_recovery_successor_session",
    ):
        assert module in import_command
    for call in run.call_args_list:
        assert call.args[0][:3] == ["/retained/env/bin/python3", "-I", "-B"]
        assert "PYTHONPATH" not in call.kwargs["env"]
        assert "PYTHONHOME" not in call.kwargs["env"]
