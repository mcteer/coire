"""Native command publication survives lost responses without duplicate effect authority."""

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest
from upgrade_native_fixtures import native_plan

from coire_core.errors import UpgradeConflict, UpgradeNotFound, UpgradeUnavailable
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    NodeUpgradeCommand,
    UpgradeWire,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("missing", [False, True])
def test_released_node_reports_only_a_verified_retained_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: bool
) -> None:
    from coire_node import upgrade_control, upgrade_runtime

    value, selected_source, candidate = native_plan(tmp_path)
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    root.write(
        "claim.json",
        NativeUpgradeClaim(
            node="coire-edge-a",
            job_id=value.operation.job_id,
            generation=value.operation.generation,
            plan_sha256=value.authority_sha256(),
            state="released",
            updated_at=datetime.now(UTC),
        ),
    )
    selected_target = selected_source.model_copy(
        update={
            "runtime": candidate,
            "release_sha256": value.target.manifest_sha256,
            "job_id": value.operation.job_id,
            "generation": value.operation.generation,
        }
    )
    settings = Settings(
        node_name="coire-edge-a",
        upgrade_bootstrap_root=str(root.root),
        upgrade_environment_root=str(tmp_path / "envs"),
    )
    control = upgrade_control.NativeUpgradeControl(settings)
    monkeypatch.setattr(control, "observed", lambda: selected_target)
    monkeypatch.setattr(control, "_guard_ready", lambda _: True)
    monkeypatch.setattr(
        control,
        "journal",
        lambda _: SimpleNamespace(
            plan=value, current=lambda: SimpleNamespace(phase="committed", source=selected_source)
        ),
    )
    if missing:
        monkeypatch.setattr(
            upgrade_runtime,
            "verify_prepared_runtime",
            Mock(side_effect=UpgradeUnavailable("Source graph absent")),
        )
    try:
        status = control.status()
        assert status.job_id is None and status.bootstrap_ready
        if missing:
            assert status.retained_source_sha256 is None and status.retained_job_id is None
            assert status.reason == "retained_source_unavailable"
        else:
            assert status.retained_source_sha256 == value.source.manifest_sha256
            assert status.retained_job_id == value.operation.job_id
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


@pytest.mark.parametrize("interrupted", [False, True])
def test_prepare_publication_and_replay_preserve_one_native_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    from coire_node import upgrade_control

    plan, selected, _ = native_plan(tmp_path)
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    root.write("runtime.json", selected.runtime)
    root.write("selection.json", selected)
    process = NodeAgentProcessIdentity(
        pid=os.getpid(),
        create_time_s=psutil.Process(os.getpid()).create_time(),
        environment_sha256=selected.runtime.environment_sha256,
    )
    root.write(
        "status.json",
        NativeGuardStatus(
            node="coire-edge-a",
            bootstrap_runtime=selected.runtime,
            process=process,
            selected=selected,
            ready=True,
            observed_at=datetime.now(UTC),
        ),
    )
    state = PrivateUpgradeStore(tmp_path / "state/upgrades")
    state.write(
        "node-process.json",
        NativeNodeProcess(selected=selected, process=process, recorded_at=datetime.now(UTC)),
    )
    settings = Settings(
        node_name="coire-edge-a",
        upgrades_enabled=True,
        upgrade_bootstrap_root=str(root.root),
        node_state_dir=str(tmp_path / "state"),
        upgrade_environment_root=str(tmp_path / "envs"),
    )
    monkeypatch.setattr(
        upgrade_control,
        "sys",
        SimpleNamespace(
            prefix=str(tmp_path / "envs/source"),
            executable=str(tmp_path / "envs/source/bin/python3"),
        ),
    )
    monkeypatch.setattr(upgrade_control, "_owned_native_process", lambda *_: Mock())
    # Catalog byte verification has separate actual-files tests; this isolates
    # command publication and lost-ack behavior after those bytes are verified.
    monkeypatch.setattr(
        upgrade_control,
        "verify_prepared_release",
        lambda _root, request: (
            plan.source if request.expected_sha256 == plan.source.manifest_sha256 else plan.target
        ),
    )
    control = upgrade_control.NativeUpgradeControl(settings)
    command = NodeUpgradeCommand.model_validate(plan.model_dump(mode="json"))
    write = PrivateUpgradeStore.write
    failed = False

    def publication(
        self: PrivateUpgradeStore, key: str, value: UpgradeWire, *, replace: bool = True
    ) -> None:
        nonlocal failed
        if interrupted and not failed and self.root == root.root and key == "claim.json":
            failed = True
            raise UpgradeUnavailable("simulated publication failure")
        write(self, key, value, replace=replace)

    monkeypatch.setattr(PrivateUpgradeStore, "write", publication)
    try:
        if interrupted:
            with pytest.raises(UpgradeUnavailable):
                control.accept(command)
            assert root.read("claim.json", NativeUpgradeClaim) is None
            with pytest.raises(UpgradeNotFound):
                control.status(plan.operation.job_id)
        accepted = control.accept(command)
        assert accepted.job_id == plan.operation.job_id and accepted.generation == 1
        claim = root.read("claim.json", NativeUpgradeClaim)
        replay = control.accept(command)
        assert replay.job_id == accepted.job_id and replay.receipt_sequence == 0
        assert root.read("claim.json", NativeUpgradeClaim) == claim
        assert control.journal(plan.operation.job_id).current().phase == "prepared"
        changed = command.model_copy(
            update={"operation": command.operation.model_copy(update={"generation": 2})}
        )
        with pytest.raises(UpgradeConflict):
            control.accept(changed)
        assert (tmp_path / "envs/current").resolve() == tmp_path / "envs/source"
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


def test_guard_readiness_uses_the_recorded_environment_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_node import upgrade_control

    _, selected, _ = native_plan(tmp_path)
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    process = NodeAgentProcessIdentity(
        pid=os.getpid(),
        create_time_s=psutil.Process().create_time(),
        environment_sha256=selected.runtime.environment_sha256,
    )
    root.write("runtime.json", selected.runtime)
    root.write(
        "status.json",
        NativeGuardStatus(
            node="coire-edge-a",
            bootstrap_runtime=selected.runtime,
            process=process,
            selected=selected,
            ready=True,
            observed_at=datetime.now(UTC),
        ),
    )
    owned = Mock(return_value=Mock())
    monkeypatch.setattr(upgrade_control, "_owned_native_process", owned)
    settings = Settings(
        node_name="coire-edge-a",
        legacy_network_mode=False,
        upgrade_bootstrap_root=str(root.root),
        upgrade_environment_root=str(tmp_path / "envs"),
    )
    try:
        assert upgrade_control.NativeUpgradeControl(settings)._guard_ready(selected)
        owned.assert_called_once_with(
            process,
            tmp_path / "envs/source/bin/python3",
            "coire_node.upgrade_guard",
        )
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


@pytest.mark.parametrize("refreshed", [False, True])
def test_released_operation_requires_helper_bound_to_current_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refreshed: bool
) -> None:
    from coire_node import upgrade_control

    value, source, target = native_plan(tmp_path)
    selected = source.model_copy(
        update={
            "runtime": target,
            "release_sha256": value.target.manifest_sha256,
            "job_id": value.operation.job_id,
            "generation": 1,
        }
    )
    bootstrap = target if refreshed else source.runtime
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    root.write("runtime.json", bootstrap)
    root.write(
        "claim.json",
        NativeUpgradeClaim(
            node="coire-edge-a",
            job_id=value.operation.job_id,
            generation=1,
            plan_sha256=value.authority_sha256(),
            state="released",
            updated_at=datetime.now(UTC),
        ),
    )
    root.write(
        "status.json",
        NativeGuardStatus(
            node="coire-edge-a",
            bootstrap_runtime=bootstrap,
            process=NodeAgentProcessIdentity(
                pid=os.getpid(),
                create_time_s=psutil.Process().create_time(),
                environment_sha256=bootstrap.environment_sha256,
            ),
            selected=selected,
            ready=True,
            observed_at=datetime.now(UTC),
        ),
    )
    owned = Mock(return_value=Mock())
    monkeypatch.setattr(upgrade_control, "_owned_native_process", owned)
    settings = Settings(
        node_name="coire-edge-a",
        legacy_network_mode=False,
        upgrade_bootstrap_root=str(root.root),
        upgrade_environment_root=str(tmp_path / "envs"),
    )
    try:
        assert upgrade_control.NativeUpgradeControl(settings)._guard_ready(selected) == refreshed
        assert owned.called == refreshed
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


def test_unresolved_predecessor_refusal_creates_no_new_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_node import upgrade_control

    plan, selected, _ = native_plan(tmp_path)
    plan = plan.model_copy(
        update={
            "operation": plan.operation.model_copy(update={"generation": 2}),
            "maintenance": plan.maintenance.model_copy(update={"generation": 2}),
        }
    )
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    prior = NativeUpgradeClaim(
        node="coire-edge-a",
        job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        generation=1,
        plan_sha256="a" * 64,
        state="released",
        updated_at=datetime.now(UTC),
    )
    root.write("claim.json", prior)
    control = upgrade_control.NativeUpgradeControl(
        Settings(node_name="coire-edge-a", upgrade_bootstrap_root=str(root.root))
    )
    monkeypatch.setattr(control, "observed", lambda: selected)
    monkeypatch.setattr(control, "_guard_ready", lambda _: True)
    refuse = Mock(
        side_effect=UpgradeConflict("Native operation has no positive terminal reconciliation")
    )
    monkeypatch.setattr(control, "verify_terminal", refuse)
    with pytest.raises(UpgradeConflict, match="positive terminal"):
        control.accept(NodeUpgradeCommand.model_validate(plan.model_dump(mode="json")))
    refuse.assert_called_once_with(prior.job_id, prior.generation)
    assert root.read("claim.json", NativeUpgradeClaim) == prior
    assert not (root.root / "operations").exists()
