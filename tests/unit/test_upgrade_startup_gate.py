"""Managed node startup waits for independent, exact-runtime recovery authority."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from coire_core.errors import UpgradeUnavailable
from coire_core.settings import Settings


def test_default_node_startup_needs_no_managed_upgrade_files(tmp_path: Path) -> None:
    from coire_node.upgrade_guard import verify_node_startup

    settings = Settings(
        node_state_dir=str(tmp_path / "state"), upgrade_bootstrap_root=str(tmp_path / "bootstrap")
    )
    assert verify_node_startup(settings) is None
    assert not (tmp_path / "bootstrap").exists()


@pytest.mark.parametrize("node", ["core", "coire-edge-a"])
def test_missing_guard_cannot_register_or_resume_managed_node(tmp_path: Path, node: str) -> None:
    from coire_node.upgrade_guard import verify_node_startup

    settings = Settings(
        upgrades_enabled=True, node_name=node, upgrade_bootstrap_root=str(tmp_path / "bootstrap")
    )
    with pytest.raises(UpgradeUnavailable):
        verify_node_startup(settings)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "stale",
        "not_ready",
        "foreign_process",
        "foreign_pointer",
        "peer_fence",
        "unclaimed_plan",
        "claimed_plan",
        "wrong_source",
        "accepted_plan",
        "claim_race",
    ],
)
def test_startup_requires_current_independent_guard_and_exact_selected_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from test_upgrade_runtime_selection import prepared

    from coire_core.models.upgrades import (
        NativeGuardStatus,
        NativeRuntimeSelection,
        NodeAgentProcessIdentity,
        PreparedNativeRuntime,
    )
    from coire_core.upgrade_journal import PrivateUpgradeStore
    from coire_node import upgrade_guard

    envs, _, runtime = prepared(tmp_path)
    selected = NativeRuntimeSelection(
        node="coire-edge-a",
        release_sha256="a" * 64,
        runtime=runtime,
        generation=0,
        selected_at=datetime.now(UTC),
    )
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    if fault in {"unclaimed_plan", "claimed_plan", "wrong_source", "accepted_plan", "claim_race"}:
        from upgrade_fixtures import plan

        from coire_core.models.upgrades import NativeUpgradeClaim, UpgradeExecutionPlan

        value = plan()
        manifest = value.source.manifest.model_copy(
            update={"environment_sha256": runtime.environment_sha256}
        )
        source = value.source.model_copy(
            update={"manifest": manifest, "manifest_sha256": manifest.sha256()}
        )
        target_manifest = value.target.manifest.model_copy(
            update={
                "compatibility": value.target.manifest.compatibility.model_copy(
                    update={"source_sha256s": (source.manifest_sha256,)}
                )
            }
        )
        target = value.target.model_copy(
            update={"manifest": target_manifest, "manifest_sha256": target_manifest.sha256()}
        )
        value = UpgradeExecutionPlan.model_validate(
            value.model_dump()
            | {
                "source": source,
                "target": target,
                "operation": value.operation.model_dump()
                | {
                    "source_sha256": source.manifest_sha256,
                    "target_sha256": target.manifest_sha256,
                },
            }
        )
        selected = selected.model_copy(
            update={
                "release_sha256": "f" * 64 if fault == "wrong_source" else source.manifest_sha256
            }
        )
        operations = PrivateUpgradeStore(root.path("operations"))
        operation = PrivateUpgradeStore(operations.path(value.operation.job_id))
        operation.write("plan.json", value)
        PrivateUpgradeStore(tmp_path / "state/upgrades").write(
            "maintenance.json", value.maintenance
        )
        if fault in {"claimed_plan", "accepted_plan"}:
            claim = NativeUpgradeClaim(
                node="coire-edge-a",
                job_id=value.operation.job_id,
                generation=value.operation.generation,
                plan_sha256=value.authority_sha256(),
                updated_at=datetime.now(UTC),
            )
            if fault == "claimed_plan":
                root.write("claim.json", claim)
            else:
                operation.write("accepted.json", claim)
    root.write("selection.json", selected)
    root.write("runtime.json", runtime)
    status = NativeGuardStatus(
        node="coire-edge-a",
        bootstrap_runtime=runtime,
        process=NodeAgentProcessIdentity(
            pid=123456, create_time_s=123.0, environment_sha256=runtime.environment_sha256
        ),
        selected=selected,
        ready=fault != "not_ready",
        observed_at=datetime.now(UTC) - timedelta(seconds=10 if fault == "stale" else 0),
    )
    root.write("status.json", status)
    (envs / "current").symlink_to(envs / ("foreign" if fault == "foreign_pointer" else "source"))
    settings = Settings(
        upgrades_enabled=True,
        node_name="coire-edge-a",
        node_state_dir=str(tmp_path / "state"),
        upgrade_bootstrap_root=str(root.root),
        upgrade_environment_root=str(envs),
    )
    if fault == "peer_fence":
        from upgrade_fixtures import plan

        PrivateUpgradeStore(tmp_path / "state/upgrades").write(
            "maintenance.json", plan().maintenance
        )
    process = Mock()
    process.create_time.return_value = 123.0
    process.is_running.return_value = True
    process.status.return_value = "running"
    process.uids.return_value = SimpleNamespace(real=os.getuid(), effective=os.getuid())
    process.exe.return_value = str(envs / "source/bin/python3")
    process.cmdline.return_value = [
        "python",
        "-m",
        "foreign" if fault == "foreign_process" else "coire_node.upgrade_guard",
    ]
    monkeypatch.setattr(upgrade_guard.psutil, "Process", lambda _: process)
    monkeypatch.setattr(upgrade_guard.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        upgrade_guard,
        "sys",
        SimpleNamespace(
            platform="darwin",
            executable=str(envs / "source/bin/python3"),
            prefix=str(envs / "source"),
        ),
    )
    if fault == "claim_race":
        from coire_node import upgrade_runtime

        original_verify = upgrade_runtime.verify_prepared_runtime
        checks = 0

        def verify_and_claim(
            environment_root: Path, interpreter_root: Path, expected: PreparedNativeRuntime
        ) -> Path:
            nonlocal checks
            result = original_verify(environment_root, interpreter_root, expected)
            checks += 1
            if checks == 2:
                root.write(
                    "claim.json",
                    NativeUpgradeClaim(
                        node="coire-edge-a",
                        job_id=value.operation.job_id,
                        generation=value.operation.generation,
                        plan_sha256=value.authority_sha256(),
                        updated_at=datetime.now(UTC),
                    ),
                )
            return result

        monkeypatch.setattr(upgrade_runtime, "verify_prepared_runtime", verify_and_claim)
    try:
        if fault in {None, "peer_fence", "unclaimed_plan"}:
            assert upgrade_guard.verify_node_startup(settings) == selected
        else:
            with pytest.raises(UpgradeUnavailable):
                upgrade_guard.verify_node_startup(settings)
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
