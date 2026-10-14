"""Independent heartbeat selects only durable known-good recovery boundaries."""

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from upgrade_native_fixtures import native_plan

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import NativeGuardStatus, NodeAgentProcessIdentity
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.fixture(autouse=True)
def restore_fixture_permissions(tmp_path: Path):
    yield
    for path in tmp_path.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o700)


@pytest.mark.parametrize(
    "fault", [None, "foreign_pointer", "foreign_bootstrap", "missing_selection"]
)
def test_guard_heartbeat_requires_retained_source_and_own_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from coire_node import upgrade_guard

    _, source, _ = native_plan(tmp_path)
    envs = tmp_path / "envs"
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    root.write("runtime.json", source.runtime)
    if fault != "missing_selection":
        root.write("selection.json", source)
    if fault == "foreign_pointer":
        (envs / "current").unlink()
        (envs / "current").symlink_to(envs / "target")
    settings = Settings(
        node_name="coire-edge-a",
        upgrades_enabled=True,
        upgrade_bootstrap_root=str(root.root),
        upgrade_environment_root=str(envs),
        node_state_dir=str(tmp_path / "state"),
    )
    monkeypatch.setattr(upgrade_guard.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        upgrade_guard,
        "sys",
        SimpleNamespace(
            platform="darwin",
            prefix=str(envs / ("target" if fault == "foreign_bootstrap" else "source")),
            executable=str(envs / "source/bin/python3"),
        ),
    )
    process = NodeAgentProcessIdentity(
        pid=os.getpid(),
        create_time_s=psutil.Process(os.getpid()).create_time(),
        environment_sha256=source.runtime.environment_sha256,
    )
    if fault:
        with pytest.raises(UpgradeUnavailable):
            upgrade_guard.publish_guard_status(settings, process)
        assert root.read("status.json", NativeGuardStatus) is None
    else:
        status = upgrade_guard.publish_guard_status(settings, process)
        assert status.ready and status.selected == source
        assert status.bootstrap_runtime == source.runtime
        assert root.read("status.json", NativeGuardStatus) == status
        assert (datetime.now(UTC) - status.observed_at).total_seconds() < 1
        assert not (root.root / "claim.json").exists()
