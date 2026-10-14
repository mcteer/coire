"""Dead serving children cannot release unknown engines or unrelated process identities."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from upgrade_native_fixtures import native_plan

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import (
    NativeSmokeAttempt,
    NativeSmokeEngineRecord,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    UpgradeInventoryEntry,
    UpgradeOwnerInventory,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.store import write_atomic_json


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "live_child",
        "wrong_slug",
        "wrong_birth",
        "foreign_fence",
        "empty_stopped",
        "empty_live",
        "empty_unreadable",
    ],
)
def test_dead_child_cleanup_uses_only_its_strict_private_normal_engine_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from coire_node import upgrade_cleanup, upgrade_guard

    plan, source, candidate = native_plan(tmp_path)
    now = datetime.now(UTC)
    journal = upgrade_guard.NativeGuardJournal(
        tmp_path / "operation",
        plan,
        source,
        candidate,
        NodeAgentProcessIdentity(
            pid=123450, create_time_s=123.0, environment_sha256=source.runtime.environment_sha256
        ),
    )
    settings = Settings(
        node_name="coire-edge-a",
        node_state_dir=str(tmp_path / "state"),
        node_store_dir=str(tmp_path / "models"),
        upgrade_environment_root=str(tmp_path / "envs"),
        upgrade_bootstrap_root=str(tmp_path / "bootstrap"),
    )
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    root.write(
        "claim.json",
        NativeUpgradeClaim(
            job_id=plan.operation.job_id,
            generation=1,
            node="coire-edge-a",
            plan_sha256=plan.authority_sha256(),
            updated_at=now,
        ),
    )
    state = PrivateUpgradeStore(tmp_path / "state/upgrades")
    state.write(
        "maintenance.json",
        plan.maintenance.model_copy(update={"state": "released"})
        if fault == "foreign_fence"
        else plan.maintenance,
    )
    attempt = NativeSmokeAttempt(
        job_id=plan.operation.job_id,
        generation=1,
        plan_sha256=plan.authority_sha256(),
        stage="candidate-smoke",
        runtime_sha256=candidate.environment_sha256,
        state="running",
        process=NodeAgentProcessIdentity(
            pid=123451, create_time_s=124.0, environment_sha256=candidate.environment_sha256
        ),
        deadline_at=now + timedelta(seconds=120),
        updated_at=now,
    )
    journal.store.write("candidate-smoke.json", attempt)
    scope = PrivateUpgradeStore(
        tmp_path
        / "state/upgrades/smokes"
        / f"{plan.operation.job_id}-1-{candidate.environment_sha256}-pre"
    )
    scope.write(
        "owner.json",
        UpgradeOwnerInventory(
            owner="engines",
            status="complete",
            entries=(
                UpgradeInventoryEntry(
                    owner="engines",
                    kind="upgrade-smoke",
                    work_id=scope.root.name,
                    state="active",
                    holds=("native-smoke-memory",),
                    process_id=123452,
                    process_created_at=125.0,
                ),
            ),
        ),
    )
    record = NativeSmokeEngineRecord(
        engine_id=uuid4(),
        slug="foreign--model" if fault == "wrong_slug" else plan.model_slug,
        port=9500,
        pid=123452,
        create_time=126.0 if fault == "wrong_birth" else 125.0,
        estimate_bytes=1024,
        started_at=now,
    )
    engine = PrivateUpgradeStore(scope.root / "engine")
    write_atomic_json(
        engine.root / "engines.json",
        [] if fault and fault.startswith("empty_") else [record.model_dump(mode="json")],
    )
    if fault and fault.startswith("empty_"):
        import psutil

        stopped = Mock()
        stopped.create_time.return_value = 125.0
        stopped.is_running.return_value = True
        stopped.status.return_value = psutil.STATUS_RUNNING
        inspect = Mock(return_value=stopped)
        if fault == "empty_stopped":
            inspect.side_effect = psutil.NoSuchProcess(123452)
        elif fault == "empty_unreadable":
            inspect.side_effect = psutil.AccessDenied(123452)
        monkeypatch.setattr(upgrade_cleanup.psutil, "Process", inspect)
    monkeypatch.setattr(
        upgrade_guard,
        "_owned_native_process",
        Mock(return_value=Mock() if fault == "live_child" else None),
    )
    monkeypatch.setattr(upgrade_cleanup, "native_process_entries", Mock(return_value=()))
    original_gone = Mock(return_value=False)
    monkeypatch.setattr(upgrade_cleanup, "owned_smoke_engine_alive", original_gone)
    try:
        if fault in {
            "wrong_slug",
            "wrong_birth",
            "foreign_fence",
            "empty_live",
            "empty_unreadable",
        }:
            with pytest.raises(UpgradeUnavailable):
                upgrade_cleanup.reconcile_smoke_cleanup(
                    settings, journal, "candidate-smoke", now + timedelta(seconds=30)
                )
            original_gone.assert_not_called()
        else:
            completed = upgrade_cleanup.reconcile_smoke_cleanup(
                settings, journal, "candidate-smoke", now + timedelta(seconds=30)
            )
            assert completed == (fault != "live_child")
            if fault in {None, "empty_stopped"}:
                retained = journal.store.read("candidate-smoke.json", NativeSmokeAttempt)
                assert retained.state == "failed" and retained.cleanup_verified
                assert retained.evidence is None
                assert scope.read("owner.json", UpgradeOwnerInventory).entries == ()
                assert (engine.root / "engines.json").read_text().strip() == "[]"
                assert (
                    original_gone.call_count >= 3
                    if fault is None
                    else original_gone.call_count == 0
                )
        if fault and fault != "empty_stopped":
            assert journal.store.read("candidate-smoke.json", NativeSmokeAttempt) == attempt
            assert scope.read("owner.json", UpgradeOwnerInventory).entries
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


@pytest.mark.parametrize(
    "fault", [None, "reused_pid", "foreign_uid", "wrong_argv", "wrong_group", "unreadable"]
)
def test_exact_smoke_process_proof_never_accepts_unrelated_or_unreadable_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    import os
    from types import SimpleNamespace

    import psutil

    from coire_node import upgrade_cleanup
    from coire_node.engines import build_engine_argv

    python = tmp_path / "retained-python"
    python.write_bytes(b"metadata-fixture")
    model = tmp_path / "models/owner--tiny"
    record = NativeSmokeEngineRecord(
        engine_id=uuid4(),
        slug="owner--tiny",
        port=9500,
        pid=123456,
        create_time=123.0,
        estimate_bytes=1024,
        started_at=datetime.now(UTC),
    )
    process = Mock()
    process.create_time.return_value = 124.0 if fault == "reused_pid" else 123.0
    process.is_running.return_value = True
    process.status.return_value = psutil.STATUS_RUNNING
    process.uids.return_value = SimpleNamespace(
        real=os.getuid() + (1 if fault == "foreign_uid" else 0), effective=os.getuid()
    )
    process.exe.return_value = str(python)
    process.cmdline.return_value = (
        ["unrelated-command"]
        if fault == "wrong_argv"
        else build_engine_argv(
            command=[str(python), "-m", "mlx_lm.server"],
            model_path=str(model),
            host="127.0.0.1",
            port=9500,
        )
    )
    inspect = Mock(return_value=process)
    if fault == "unreadable":
        inspect.side_effect = psutil.AccessDenied(record.pid)
    monkeypatch.setattr(upgrade_cleanup.psutil, "Process", inspect)
    monkeypatch.setattr(
        upgrade_cleanup.os,
        "getpgid",
        Mock(return_value=123457 if fault == "wrong_group" else record.pid),
    )
    if fault in {None, "reused_pid"}:
        assert upgrade_cleanup.owned_smoke_engine_alive(record, python, python, model) == (
            fault is None
        )
        if fault == "reused_pid":
            process.cmdline.assert_not_called()
    else:
        with pytest.raises(UpgradeUnavailable):
            upgrade_cleanup.owned_smoke_engine_alive(record, python, python, model)
