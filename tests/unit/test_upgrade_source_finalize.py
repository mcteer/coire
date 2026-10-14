"""Restored serving proof cannot hide physical staging or child ownership."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from upgrade_native_fixtures import native_plan

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import (
    NativeRecoveryIntent,
    NativeSmokeAttempt,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    NodeUpgradeInventory,
    NodeUpgradeStatus,
    UpgradeEvidence,
    UpgradeInventoryEntry,
    UpgradeOwnerInventory,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "busy", "missing_hold", "unknown_child", "terminal_fsync"])
def test_source_terminal_requires_physical_cleanup_and_replays_partial_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from coire_node import upgrade_cleanup, upgrade_finalize, upgrade_guard
    from coire_node.maintenance import REQUIRED_OWNERS
    from coire_node.upgrade_executor import NativeDiskReservation

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
    intent = NativeRecoveryIntent.model_validate(
        journal.current().model_dump(mode="json")
        | {
            "phase": "source_verified",
            "activation_started_at": now,
            "activation_grant_id": uuid4(),
            "readiness_deadline_at": now + timedelta(seconds=120),
            "recovery_started_at": now,
            "recovery_deadline_at": now + timedelta(seconds=300),
        }
    )
    journal.store.write("intent.json", intent)
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
    state.write("maintenance.json", plan.maintenance)
    ledger = NativeDiskReservation(
        tmp_path / "envs/.upgrade-reservations" / plan.operation.job_id, plan
    )
    hold = ledger.reserve(32 * 1024)
    if fault == "missing_hold":
        ledger.store.path("hold.json").unlink()
    proof = UpgradeEvidence(
        check="node",
        runtime_sha256=source.runtime.environment_sha256,
        source_verified=True,
        cleanup_verified=True,
        model_id=plan.model_id,
        model_variant_id=plan.model_variant_id,
        model_manifest_sha256=plan.model_manifest_sha256,
        generated_tokens=10,
        output_sha256="a" * 64,
    )
    verified = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=1,
        sequence=1,
        phase=UpgradePhase.VERIFYING_SOURCE,
        effect_id="source-serving",
        occurred_at=now,
        outcome="complete",
        observed_sha256=plan.source.manifest_sha256,
        evidence=proof.model_dump_json(),
    )
    journal.receipts.append(verified)
    journal.store.write(
        "source-serving.json",
        NativeSmokeAttempt(
            job_id=plan.operation.job_id,
            generation=1,
            plan_sha256=plan.authority_sha256(),
            stage="source-serving",
            runtime_sha256=source.runtime.environment_sha256,
            state="complete",
            process=NodeAgentProcessIdentity(
                pid=123451,
                create_time_s=124.0,
                environment_sha256=source.runtime.environment_sha256,
            ),
            evidence=proof,
            cleanup_verified=True,
            deadline_at=intent.recovery_deadline_at,
            updated_at=now,
        ),
    )
    scope = PrivateUpgradeStore(
        tmp_path
        / "state/upgrades/smokes"
        / f"{plan.operation.job_id}-1-{source.runtime.environment_sha256}-source"
    )
    scope.write(
        "owner.json",
        UpgradeOwnerInventory(
            owner="engines",
            status="complete",
            entries=(
                UpgradeInventoryEntry(
                    owner="engines",
                    kind="unknown",
                    work_id="unknown",
                    state="unknown",
                ),
            )
            if fault == "unknown_child"
            else (),
        ),
    )
    scope.write("result.json", proof)
    monkeypatch.setattr(upgrade_guard, "_owned_native_process", Mock(return_value=None))
    monkeypatch.setattr(upgrade_cleanup, "native_process_entries", Mock(return_value=()))
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
        owners=()
        if fault == "busy"
        else tuple(
            UpgradeOwnerInventory(owner=name, status="complete") for name in REQUIRED_OWNERS
        ),
    )
    append = journal.receipts.append
    failure = Mock(side_effect=UpgradeUnavailable("simulated terminal fsync failure"))
    try:
        if fault == "terminal_fsync":
            monkeypatch.setattr(journal.receipts, "append", failure)
            with pytest.raises(UpgradeUnavailable):
                upgrade_finalize.finalize_restored_source(settings, journal, transport)
            assert ledger.current().state == "released"
            assert journal.receipts.latest() == verified
            monkeypatch.setattr(journal.receipts, "append", append)
        if fault in {"missing_hold", "unknown_child"}:
            with pytest.raises(UpgradeUnavailable):
                upgrade_finalize.finalize_restored_source(settings, journal, transport)
            assert journal.receipts.latest() == verified
            assert ledger.store.path(hold.resource_key).exists()
        elif fault == "busy":
            assert upgrade_finalize.finalize_restored_source(settings, journal, transport) is None
            assert ledger.current().state == "held"
            assert journal.receipts.latest() == verified
        else:
            terminal = upgrade_finalize.finalize_restored_source(settings, journal, transport)
            assert terminal is not None and terminal.phase is UpgradePhase.ROLLED_BACK
            assert terminal.restoration == "verified" and terminal.outcome == "complete"
            assert ledger.current().state == "released"
            assert not ledger.store.path(hold.resource_key).exists()
            assert upgrade_finalize.finalize_restored_source(settings, journal, transport) is None
            assert journal.receipts.latest() == terminal
        assert journal.current() == intent
        assert state.read("maintenance.json", type(plan.maintenance)) == plan.maintenance
        assert root.read("claim.json", NativeUpgradeClaim).state == "active"
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
