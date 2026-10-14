"""Recovery preserves the source and refuses foreign or unconfirmed candidate stops."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from upgrade_native_fixtures import native_plan

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import (
    NativeNodeProcess,
    NativeRecoveryIntent,
    NativeRuntimeSelection,
    NodeAgentProcessIdentity,
    NodeUpgradeInventory,
    UpgradeOwnerInventory,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "broken_candidate",
        "missing_inventory",
        "unconfirmed_stop",
        "foreign_operation",
        "missing_source",
        "expired",
        "boundary_fence_changed",
    ],
)
def test_restore_requires_only_retained_source_and_owned_candidate_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from coire_node import upgrade_executor, upgrade_guard, upgrade_runtime
    from coire_node.maintenance import REQUIRED_OWNERS

    plan, source, candidate = native_plan(tmp_path)
    original = NodeAgentProcessIdentity(
        pid=123455, create_time_s=123.0, environment_sha256=source.runtime.environment_sha256
    )
    journal = upgrade_guard.NativeGuardJournal(
        tmp_path / "operation", plan, source, candidate, original
    )
    now = datetime.now(UTC)
    activated_at = now - timedelta(seconds=500 if fault == "expired" else 1)
    recovery_at = now - timedelta(seconds=400 if fault == "expired" else 0)
    intent = NativeRecoveryIntent.model_validate(
        journal.current().model_dump(mode="json")
        | {
            "phase": "restoring",
            "activation_started_at": activated_at,
            "activation_grant_id": uuid4(),
            "readiness_deadline_at": activated_at + timedelta(seconds=120),
            "recovery_started_at": recovery_at,
            "recovery_deadline_at": recovery_at + timedelta(seconds=300),
        }
    )
    journal.store.write("intent.json", intent)
    selected = NativeRuntimeSelection(
        node="coire-edge-a",
        runtime=candidate,
        release_sha256=plan.target.manifest_sha256,
        generation=1,
        job_id="01ARZ3NDEKTSV4RRFFQ69G5FAW"
        if fault == "foreign_operation"
        else plan.operation.job_id,
        selected_at=now,
    )
    process = NodeAgentProcessIdentity(
        pid=123456, create_time_s=124.0, environment_sha256=candidate.environment_sha256
    )
    state = PrivateUpgradeStore(tmp_path / "state/upgrades")
    state.write("maintenance.json", plan.maintenance)
    state.write(
        "node-process.json", NativeNodeProcess(selected=selected, process=process, recorded_at=now)
    )
    (tmp_path / "envs/current").unlink()
    (tmp_path / "envs/current").symlink_to(tmp_path / "envs/target")
    if fault in {"broken_candidate", "missing_source"}:
        directory = tmp_path / "envs" / ("target" if fault == "broken_candidate" else "source")
        directory.chmod(0o700)
        (directory / "module.py").unlink()
        directory.chmod(0o500)
    inventory = NodeUpgradeInventory(
        job_id=plan.operation.job_id,
        generation=1,
        node="coire-edge-a",
        runtime_sha256=candidate.environment_sha256,
        observed_at=now,
        owners=tuple(
            UpgradeOwnerInventory(owner=name, status="complete", entries=())
            for name in REQUIRED_OWNERS
        ),
    )
    settings = Settings(
        node_name="coire-edge-a",
        node_state_dir=str(tmp_path / "state"),
        upgrade_environment_root=str(tmp_path / "envs"),
    )
    alive = None if fault == "broken_candidate" else Mock()
    monkeypatch.setattr(upgrade_guard, "_owned_node_process", lambda *_: alive)
    stop = Mock(return_value=fault != "unconfirmed_stop")
    monkeypatch.setattr(upgrade_guard, "stop_owned_node_process", stop)
    monkeypatch.setattr(upgrade_executor.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(upgrade_executor, "is_darwin", lambda _: True)
    if fault == "boundary_fence_changed":
        reconcile = upgrade_runtime.reconcile_native_selection

        def change_before_reconcile(*args: object, **kwargs: object) -> object:
            state.write(
                "maintenance.json",
                plan.maintenance.model_copy(update={"state": "released"}),
            )
            return reconcile(*args, **kwargs)

        monkeypatch.setattr(upgrade_runtime, "reconcile_native_selection", change_before_reconcile)
    try:
        if fault in {None, "broken_candidate"}:
            result = upgrade_executor.restore_native_source(settings, journal, inventory)
            assert result.phase == "source_selected"
            assert result.recovery_deadline_at == intent.recovery_deadline_at
            assert (tmp_path / "envs/current").resolve() == tmp_path / "envs/source"
            assert stop.call_count == (0 if fault == "broken_candidate" else 1)
        else:
            with pytest.raises(UpgradeUnavailable):
                upgrade_executor.restore_native_source(
                    settings, journal, None if fault == "missing_inventory" else inventory
                )
            assert journal.current().phase == "restoring"
            assert (tmp_path / "envs/current").resolve() == tmp_path / "envs/target"
            assert stop.call_count == (
                1 if fault in {"unconfirmed_stop", "boundary_fence_changed"} else 0
            )
        if fault != "boundary_fence_changed":
            assert state.read("maintenance.json", type(plan.maintenance)).state != "released"
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
