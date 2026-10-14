"""Serving child authority is delivered only after durable exact process ownership."""

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from upgrade_native_fixtures import native_plan

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    NativeSmokeAttempt,
    NativeSmokeCommand,
    NodeAgentProcessIdentity,
    UpgradeEvidence,
    UpgradeInventoryEntry,
    UpgradeOwnerInventory,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "identity_persist", "exchange", "cleanup", "expired"])
def test_child_input_follows_durable_ownership_and_replay_never_respawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from coire_node import upgrade_child, upgrade_guard, upgrade_inventory

    monkeypatch.setenv("NODE_TOKEN", "must-not-reach-engine")
    monkeypatch.setenv("UPGRADE_EXECUTOR_TOKEN", "must-not-reach-engine")
    monkeypatch.setenv("DATABASE_URL", "must-not-reach-engine")

    plan, source, candidate = native_plan(tmp_path)
    journal = upgrade_guard.NativeGuardJournal(
        tmp_path / "operation",
        plan,
        source,
        candidate,
        NodeAgentProcessIdentity(
            pid=123455, create_time_s=123.0, environment_sha256=source.runtime.environment_sha256
        ),
    )
    settings = Settings(
        node_name="coire-edge-a",
        node_state_dir=str(tmp_path / "state"),
        upgrade_environment_root=str(tmp_path / "envs"),
    )
    command = NativeSmokeCommand(
        plan=plan,
        runtime_sha256=candidate.environment_sha256,
        deadline_at=datetime.now(UTC) + timedelta(seconds=-1 if fault == "expired" else 120),
    )
    child = Mock(pid=123456, stdin=BytesIO(), stdout=BytesIO())
    spawn = Mock(return_value=child)
    monkeypatch.setattr(upgrade_child.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        upgrade_child.psutil, "Process", lambda _: SimpleNamespace(create_time=lambda: 123.0)
    )
    monkeypatch.setattr(upgrade_guard, "_owned_native_process", lambda *_: None)
    monkeypatch.setattr(upgrade_inventory, "native_process_entries", lambda *_: ())
    evidence = UpgradeEvidence(
        check="node",
        runtime_sha256=candidate.environment_sha256,
        model_id=plan.model_id,
        model_variant_id=plan.model_variant_id,
        model_manifest_sha256=plan.model_manifest_sha256,
        generated_tokens=10,
        output_sha256="a" * 64,
        cleanup_verified=True,
    )
    exchanged = False

    def exchange(_child: object, payload: bytes, deadline: datetime) -> bytes:
        nonlocal exchanged
        exchanged = True
        recorded = journal.store.read("candidate-smoke.json", NativeSmokeAttempt)
        assert recorded is not None and recorded.state == "running" and recorded.process is not None
        assert recorded.process.pid == child.pid and recorded.deadline_at == deadline
        assert NativeSmokeCommand.model_validate_json(payload) == command
        if fault == "exchange":
            raise UpgradeUnavailable("lost child response")
        scope = PrivateUpgradeStore(
            Path(settings.node_state_dir)
            / "upgrades/smokes"
            / f"{plan.operation.job_id}-1-{candidate.environment_sha256}-pre"
        )
        entries = (
            (
                UpgradeInventoryEntry(
                    owner="engines", kind="upgrade-smoke", work_id="held", state="unknown"
                ),
            )
            if fault == "cleanup"
            else ()
        )
        scope.write(
            "owner.json", UpgradeOwnerInventory(owner="engines", status="complete", entries=entries)
        )
        scope.write("result.json", evidence)
        return evidence.model_dump_json().encode()

    monkeypatch.setattr(upgrade_guard, "exchange_smoke_child", exchange)
    write = PrivateUpgradeStore.write

    def persist(
        self: PrivateUpgradeStore, key: str, value: object, *, replace: bool = True
    ) -> None:
        if (
            fault == "identity_persist"
            and isinstance(value, NativeSmokeAttempt)
            and value.state == "running"
        ):
            raise UpgradeUnavailable("identity fsync failed")
        write(self, key, value, replace=replace)

    monkeypatch.setattr(PrivateUpgradeStore, "write", persist)
    try:
        if fault:
            with pytest.raises(UpgradeUnavailable):
                upgrade_child.run_guard_smoke(settings, journal, command)
            assert exchanged == (fault in {"exchange", "cleanup"})
            if fault == "expired":
                spawn.assert_not_called()
            else:
                child.terminate.assert_not_called()
                assert child.stdin.closed and child.stdout.closed
            if fault == "exchange":
                from coire_node import upgrade_cleanup

                cleanup = Mock(return_value=False)
                monkeypatch.setattr(upgrade_cleanup, "reconcile_smoke_cleanup", cleanup)
                before = journal.store.read("candidate-smoke.json", NativeSmokeAttempt)
                with pytest.raises(UpgradeConflict):
                    upgrade_child.run_guard_smoke(
                        settings,
                        journal,
                        command.model_copy(
                            update={"deadline_at": command.deadline_at + timedelta(seconds=1)}
                        ),
                    )
                cleanup.assert_not_called()
                with pytest.raises(UpgradeUnavailable):
                    upgrade_child.run_guard_smoke(settings, journal, command)
                cleanup.assert_called_once_with(
                    settings, journal, "candidate-smoke", command.deadline_at
                )
                assert journal.store.read("candidate-smoke.json", NativeSmokeAttempt) == before
                spawn.assert_called_once()
        else:
            assert upgrade_child.run_guard_smoke(settings, journal, command) == evidence
            assert upgrade_inventory.upgrade_smoke_entries(Path(settings.node_state_dir)) == ()
            assert upgrade_child.run_guard_smoke(settings, journal, command) == evidence
            spawn.assert_called_once()
            assert spawn.call_args.args[0][-4:] == ["-I", "-B", "-m", "coire_node.upgrade_smoke"]
            child_environment = spawn.call_args.kwargs["env"]
            assert "NODE_TOKEN" not in child_environment
            assert "UPGRADE_EXECUTOR_TOKEN" not in child_environment
            assert "DATABASE_URL" not in child_environment
            for field in (
                "node_name",
                "node_state_dir",
                "node_store_dir",
                "upgrade_environment_root",
            ):
                # The child constructs the actual shared Settings model; these
                # names must be recognized, even with stale inherited values.
                monkeypatch.setenv(field.upper(), child_environment[field.upper()])
            inherited = Settings()
            assert inherited.node_name == settings.node_name
            assert inherited.node_state_dir == settings.node_state_dir
            assert inherited.node_store_dir == settings.node_store_dir
            assert inherited.upgrade_environment_root == settings.upgrade_environment_root
            with pytest.raises(UpgradeConflict):
                upgrade_child.run_guard_smoke(
                    settings,
                    journal,
                    command.model_copy(
                        update={"deadline_at": command.deadline_at + timedelta(seconds=1)}
                    ),
                )
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
