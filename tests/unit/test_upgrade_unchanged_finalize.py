"""Pre-activation abort resumes a known source without stopping existing work."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from upgrade_native_fixtures import native_plan

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import (
    NativeAbortIntent,
    NativeRecoveryIntent,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    NodeUpgradeCancel,
    NodeUpgradeInventory,
    NodeUpgradeStatus,
    UpgradeInventoryEntry,
    UpgradeOwnerInventory,
    UpgradePhase,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "fault",
    [None, "unknown", "intent_fsync", "expired", "fence_changed", "late_drain", "late_cancel"],
)
def test_cancelled_source_closes_with_known_requests_and_fixed_abort_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from coire_node import upgrade_finalize, upgrade_guard
    from coire_node.maintenance import REQUIRED_OWNERS

    plan, source, candidate = native_plan(tmp_path)
    if fault == "late_drain":
        accepted = datetime.now(UTC) - timedelta(seconds=1201)
        plan = plan.model_copy(
            update={
                "operation": plan.operation.model_copy(
                    update={"accepted_at": accepted, "updated_at": accepted}
                ),
                "maintenance": plan.maintenance.model_copy(
                    update={
                        "updated_at": accepted,
                        "drain_deadline_at": accepted + timedelta(seconds=900),
                    }
                ),
            }
        )
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
        upgrade_bootstrap_root=str(tmp_path / "bootstrap"),
        upgrade_environment_root=str(tmp_path / "envs"),
    )
    now = datetime.now(UTC)
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
    state.write("maintenance.json", plan.maintenance)
    transport = Mock()
    transport.status.return_value = NodeUpgradeStatus(
        node="coire-edge-a",
        job_id=plan.operation.job_id,
        generation=1,
        release_sha256=plan.source.manifest_sha256,
        runtime_sha256=source.runtime.environment_sha256,
        environment_id=source.runtime.environment_id,
        bootstrap_ready=True,
        observed_at=now,
    )
    transport.inventory.return_value = NodeUpgradeInventory(
        job_id=plan.operation.job_id,
        generation=1,
        node="coire-edge-a",
        runtime_sha256=source.runtime.environment_sha256,
        observed_at=now,
        owners=tuple(
            UpgradeOwnerInventory(
                owner=name,
                status="complete",
                entries=(
                    UpgradeInventoryEntry(
                        owner=name,
                        kind="existing-work",
                        work_id="existing",
                        state="unknown" if fault == "unknown" else "active",
                    ),
                )
                if name == "requests"
                else (),
            )
            for name in REQUIRED_OWNERS
        ),
    )
    persist = PrivateUpgradeStore.write
    interrupted = False

    def fail_intent(
        self: PrivateUpgradeStore, key: str, value: object, *, replace: bool = True
    ) -> None:
        nonlocal interrupted
        if (
            fault == "intent_fsync"
            and not interrupted
            and isinstance(value, NativeRecoveryIntent)
            and value.phase == "unchanged_verified"
        ):
            interrupted = True
            raise UpgradeUnavailable("simulated terminal intent fsync failure")
        persist(self, key, value, replace=replace)

    monkeypatch.setattr(PrivateUpgradeStore, "write", fail_intent)
    if fault == "expired":
        captured_at = now - timedelta(seconds=301)
        journal.store.write(
            "abort.json",
            NativeAbortIntent(
                job_id=plan.operation.job_id,
                generation=1,
                plan_sha256=plan.authority_sha256(),
                reason="cancelled",
                captured_at=captured_at,
                deadline_at=captured_at + timedelta(seconds=300),
            ),
        )
    if fault == "late_cancel":
        captured_at = now - timedelta(seconds=301)
        journal.root.write("cancel.json", NodeUpgradeCancel(generation=1))
        os.utime(
            journal.root.path("cancel.json"), (captured_at.timestamp(), captured_at.timestamp())
        )
    if fault == "late_drain":
        captured_at = plan.maintenance.drain_deadline_at
    if fault == "fence_changed":

        def change_fence(*args: object, **kwargs: object) -> NodeUpgradeInventory:
            state.write(
                "maintenance.json",
                plan.maintenance.model_copy(update={"generation": 2}),
            )
            return transport.inventory.return_value

        transport.inventory.side_effect = change_fence
    try:
        if fault == "fence_changed":
            from coire_core.errors import UpgradeConflict

            with pytest.raises(UpgradeConflict):
                upgrade_finalize.finalize_unchanged_source(
                    settings, journal, transport, "cancelled"
                )
            assert journal.current().phase == "prepared"
            assert journal.receipts.latest() is None
            assert not (tmp_path / "envs/.upgrade-reservations").exists()
            return
        if fault in {"expired", "late_drain", "late_cancel"}:
            from coire_node.upgrade_workflow import advance_guard_step

            receipt = upgrade_finalize.finalize_unchanged_source(
                settings,
                journal,
                transport,
                "drain_deadline" if fault == "late_drain" else "cancelled",
            )
            assert receipt.phase is UpgradePhase.RECOVERY_REQUIRED
            retained = journal.current()
            assert retained.activation_started_at is None
            assert retained.recovery_deadline_at == captured_at + timedelta(seconds=300)
            assert advance_guard_step(settings, journal, transport) is None
            assert journal.current() == retained
            transport.status.assert_not_called()
            transport.inventory.assert_not_called()
            assert state.read("maintenance.json", type(plan.maintenance)) == plan.maintenance
            assert root.read("claim.json", NativeUpgradeClaim).state == "active"
            assert (tmp_path / "envs/current").resolve() == tmp_path / "envs/source"
            return
        if fault == "intent_fsync":
            with pytest.raises(UpgradeUnavailable):
                upgrade_finalize.finalize_unchanged_source(
                    settings, journal, transport, "cancelled"
                )
            assert journal.current().phase == "prepared"
            assert journal.receipts.latest().phase is UpgradePhase.CANCELLED_UNCHANGED
        terminal = upgrade_finalize.finalize_unchanged_source(
            settings, journal, transport, "cancelled"
        )
        captured = journal.store.read("abort.json", NativeAbortIntent)
        assert captured is not None
        if fault == "unknown":
            assert terminal is None and journal.current().phase == "prepared"
            assert journal.receipts.latest() is None
        else:
            assert journal.current().phase == "unchanged_verified"
            assert journal.current().activation_started_at is None
            assert journal.receipts.latest().phase is UpgradePhase.CANCELLED_UNCHANGED
        assert (
            upgrade_finalize.finalize_unchanged_source(
                settings, journal, transport, "candidate_failed"
            )
            is None
        )
        assert journal.store.read("abort.json", NativeAbortIntent) == captured
        assert state.read("maintenance.json", type(plan.maintenance)) == plan.maintenance
        assert root.read("claim.json", NativeUpgradeClaim).state == "active"
        assert (tmp_path / "envs/current").resolve() == tmp_path / "envs/source"
        assert not (tmp_path / "envs/.upgrade-reservations").exists()
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
