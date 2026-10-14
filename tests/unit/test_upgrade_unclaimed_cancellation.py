"""Actual private namespaces/flocks: refusing unknown effects and preserving archive bytes."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import psutil
import pytest
from upgrade_native_fixtures import native_plan

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_unclaimed import (
    UpgradeUnclaimedCancellationGrant,
    UpgradeUnclaimedCancellationState,
)
from coire_core.models.upgrades import (
    NativeNodeProcess,
    NativeRecoveryIntent,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    NodeUpgradeCommand,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node import upgrade_unclaimed_cancellation as support
from coire_node.upgrade_guard import NativeGuardJournal


@pytest.mark.parametrize("transport_reissued", [False, True])
@pytest.mark.parametrize("fence_state", ["fencing", "active"])
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "unknown",
        "claimed",
        "reservation",
        "authority",
        "process",
        "lost_closure",
        "deadline",
        "activation",
        "hardlink",
    ],
)
def test_only_exact_unpublished_preparation_can_be_archived(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
    fence_state: str,
    transport_reissued: bool,
) -> None:
    plan, selected, candidate = native_plan(tmp_path)
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    operations = PrivateUpgradeStore(root.root / "operations")
    operation = PrivateUpgradeStore(operations.root / plan.operation.job_id)
    assert plan.authorization_expires_at is not None
    recorded_plan = (
        plan.model_copy(
            update={
                "authorization_expires_at": plan.authorization_expires_at + timedelta(seconds=1)
            }
        )
        if transport_reissued
        else plan
    )
    operation.write("plan.json", recorded_plan)
    process = NodeAgentProcessIdentity(
        pid=os.getpid(),
        create_time_s=psutil.Process().create_time(),
        environment_sha256=selected.runtime.environment_sha256,
    )
    NativeGuardJournal(operation.root, recorded_plan, selected, candidate, process)
    operation.write(
        "command.json", NodeUpgradeCommand.model_validate(recorded_plan.model_dump(mode="json"))
    )
    state = PrivateUpgradeStore(tmp_path / "state/upgrades")
    state.write(
        "node-process.json",
        NativeNodeProcess(selected=selected, process=process, recorded_at=datetime.now(UTC)),
    )
    state.write("maintenance.json", plan.maintenance.model_copy(update={"state": fence_state}))
    settings = Settings(
        node_name="coire-edge-a",
        upgrade_bootstrap_root=str(root.root),
        upgrade_environment_root=str(tmp_path / "envs"),
        node_state_dir=str(tmp_path / "state"),
    )
    monkeypatch.setattr(support.SupportCancellationControl, "observed", lambda _: selected)
    monkeypatch.setattr(support.SupportCancellationControl, "_guard_ready", lambda *_: True)
    monkeypatch.setattr(
        support, "native_process_entries", lambda *_: [Mock()] if fault == "process" else ()
    )
    grant = UpgradeUnclaimedCancellationGrant(
        plan=plan, requested_by=uuid4(), requested_at=datetime.now(UTC)
    )
    transport = Mock()
    transport._read.return_value = UpgradeUnclaimedCancellationState(
        grant=grant, observed_at=datetime.now(UTC)
    )
    if fault == "unknown":
        operation.path("activation.json").write_bytes(b"unknown")
    elif fault == "claimed":
        root.write(
            "claim.json",
            NativeUpgradeClaim(
                job_id=plan.operation.job_id,
                generation=1,
                node="coire-edge-a",
                plan_sha256=plan.authority_sha256(),
                updated_at=datetime.now(UTC),
            ),
        )
    elif fault == "reservation":
        (tmp_path / "envs/.upgrade-reservations" / plan.operation.job_id).mkdir(parents=True)
    elif fault == "authority":
        transport._read.return_value = transport._read.return_value.model_copy(
            update={"grant": grant.model_copy(update={"requested_by": uuid4()})}
        )
    if fault == "deadline":
        operation.write(
            "plan.json",
            recorded_plan.model_copy(
                update={
                    "maintenance": recorded_plan.maintenance.model_copy(
                        update={
                            "drain_deadline_at": recorded_plan.maintenance.drain_deadline_at
                            + timedelta(seconds=1)
                        }
                    )
                }
            ),
        )
    elif fault == "activation":
        guard = PrivateUpgradeStore(operation.root / "guard")
        intent = guard.read("intent.json", NativeRecoveryIntent)
        assert intent is not None
        guard.write("intent.json", intent.model_copy(update={"phase": "activation_intent"}))
    elif fault == "hardlink":
        os.link(operation.path("command.json"), tmp_path / "foreign-command-link")
    before = {
        p.relative_to(operation.root).as_posix(): p.read_bytes()
        for p in operation.root.rglob("*")
        if p.is_file()
    }
    try:
        if fault not in {None, "lost_closure"}:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                support.cancel_unclaimed(settings, grant, transport)
            assert operation.root.exists()
            assert {
                p.relative_to(operation.root).as_posix(): p.read_bytes()
                for p in operation.root.rglob("*")
                if p.is_file()
            } == before
            assert not root.path("unclaimed-" + plan.operation.job_id + ".json").exists()
            return
        if fault == "lost_closure":
            close = support.SupportCancellationControl.cancel_unstarted
            calls = 0

            def disconnect(self, *args):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise UpgradeUnavailable("lost before closure")
                return close(self, *args)

            monkeypatch.setattr(support.SupportCancellationControl, "cancel_unstarted", disconnect)
            with pytest.raises(UpgradeUnavailable, match="before closure"):
                support.cancel_unclaimed(settings, grant, transport)
        result = support.cancel_unclaimed(settings, grant, transport)
        assert result.phase.value == "cancelled_unchanged"
        archive = root.root / "unclaimed-preparations" / plan.operation.job_id
        assert not operation.root.exists()
        assert {
            p.relative_to(archive).as_posix(): p.read_bytes()
            for p in archive.rglob("*")
            if p.is_file()
        } == before
        replay = support.cancel_unclaimed(settings, grant, transport)
        assert replay.receipts == result.receipts
        assert not root.path("claim.json").exists()
        assert (tmp_path / "envs/current").resolve() == tmp_path / "envs/source"
    finally:
        for p in tmp_path.rglob("*"):
            if p.is_dir() and not p.is_symlink():
                p.chmod(0o700)
