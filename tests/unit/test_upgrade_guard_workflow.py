"""Preparation cannot turn busy or unpublished ownership into execution authority."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from upgrade_native_fixtures import native_plan

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    NativeRecoveryIntent,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    NodeUpgradeCancel,
    NodeUpgradeCommand,
    NodeUpgradeInventory,
    NodeUpgradeStatus,
    UpgradeActivationGrant,
    UpgradeEvidence,
    UpgradeOwnerInventory,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.maintenance import REQUIRED_OWNERS


@pytest.mark.parametrize("outcome", ["success", "failed", "expired"])
@pytest.mark.parametrize("stage", ["candidate", "source"])
def test_pending_collective_keeps_original_epoch_without_accepting_failed_or_expired_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, stage: str
) -> None:
    from coire_core.models import LinkObservation, ProbeOutcome, ProbeTransport
    from coire_core.models.upgrade_collective import NodeUpgradeCollectiveResult
    from coire_node import upgrade_workflow
    from coire_node.upgrade_guard import NativeGuardJournal

    plan, source, candidate = native_plan(tmp_path, paired=True)
    now = datetime.now(UTC)
    journal = NativeGuardJournal(
        tmp_path / "operation",
        plan,
        source,
        candidate,
        NodeAgentProcessIdentity(
            pid=123450, create_time_s=123.0, environment_sha256=source.runtime.environment_sha256
        ),
    )
    settings = Settings(
        node_name="coire-edge-a", upgrade_bootstrap_root=str(tmp_path / "bootstrap")
    )
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    root.write(
        "claim.json",
        NativeUpgradeClaim(
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            node="coire-edge-a",
            plan_sha256=plan.authority_sha256(),
            updated_at=now,
        ),
    )
    selected = candidate if stage == "candidate" else source.runtime
    release = plan.target if stage == "candidate" else plan.source
    phase = UpgradePhase.VERIFYING if stage == "candidate" else UpgradePhase.VERIFYING_SOURCE
    verify = (
        upgrade_workflow.verify_candidate_step
        if stage == "candidate"
        else upgrade_workflow.verify_source_step
    )
    intent = NativeRecoveryIntent.model_validate(
        journal.current().model_dump(mode="json")
        | {
            "phase": "candidate_selected" if stage == "candidate" else "source_selected",
            "activation_started_at": now,
            "activation_grant_id": str(uuid4()),
            "readiness_deadline_at": now + timedelta(seconds=120),
            **(
                {"recovery_started_at": now, "recovery_deadline_at": now + timedelta(seconds=300)}
                if stage == "source"
                else {}
            ),
        }
    )
    journal.store.write("intent.json", intent)
    receipt = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=1,
        sequence=1,
        phase=phase,
        effect_id="native-serving",
        outcome="intent",
        observed_sha256=release.manifest_sha256,
        occurred_at=now,
    )
    journal.receipts.append(receipt)
    transport = Mock()
    transport.status.return_value = NodeUpgradeStatus(
        node="coire-edge-a",
        job_id=plan.operation.job_id,
        generation=1,
        release_sha256=release.manifest_sha256,
        runtime_sha256=selected.environment_sha256,
        environment_id=selected.environment_id,
        bootstrap_ready=True,
        observed_at=now,
    )
    transport.collective.side_effect = UpgradeUnavailable("Live owner observation unavailable")
    proof = UpgradeEvidence(
        check="node",
        runtime_sha256=selected.environment_sha256,
        model_id=plan.model_id,
        model_variant_id=plan.model_variant_id,
        model_manifest_sha256=plan.model_manifest_sha256,
        generated_tokens=10,
        output_sha256="a" * 64,
        cleanup_verified=True,
    )
    smoke = Mock(return_value=proof)
    monkeypatch.setattr(upgrade_workflow, "run_guard_smoke", smoke)
    try:
        assert verify(settings, journal, transport) is None
        assert journal.current() == intent
        assert journal.receipts.latest() == receipt
        assert journal.store.read(stage + "-collective.json", NodeUpgradeCollectiveResult) is None
        deadline = (
            intent.readiness_deadline_at if stage == "candidate" else intent.recovery_deadline_at
        )
        assert transport.collective.call_args.kwargs["deadline_at"] == deadline
        if outcome == "expired":
            elapsed = now - timedelta(seconds=121)
            expired = NativeRecoveryIntent.model_validate(
                intent.model_dump(mode="json")
                | {
                    "activation_started_at": elapsed,
                    "readiness_deadline_at": elapsed + timedelta(seconds=120),
                    **(
                        {
                            "recovery_started_at": now - timedelta(seconds=301),
                            "recovery_deadline_at": now - timedelta(seconds=1),
                        }
                        if stage == "source"
                        else {}
                    ),
                }
            )
            journal.store.write("intent.json", expired)
            with pytest.raises(UpgradeUnavailable):
                verify(settings, journal, transport)
            assert transport.collective.call_count == 1 and smoke.call_count == 1
        else:
            succeeded = outcome == "success"
            result = NodeUpgradeCollectiveResult(
                job_id=plan.operation.job_id,
                generation=1,
                plan_sha256=plan.authority_sha256(),
                stage=stage,
                deadline_at=deadline,
                observations=tuple(
                    LinkObservation(
                        id=uuid4(),
                        node_a="coire-edge-a",
                        node_b="coire-edge-b",
                        transport=ProbeTransport.JACCL,
                        outcome=ProbeOutcome.SUCCEEDED if succeeded else ProbeOutcome.FAILED,
                        bandwidth_bytes_per_second=9_000_000_000 if succeeded else None,
                        latency_ms=0.5 if succeeded else None,
                        os_version_a="26.2",
                        os_version_b="26.2",
                        engine_version="0.32.2",
                        reason=None if succeeded else "collective failed",
                        runtime_sha256_a=selected.environment_sha256,
                        runtime_sha256_b=plan.peer_runtime_sha256,
                        observed_at=datetime.now(UTC),
                    )
                    for _ in range(3)
                ),
            )
            transport.collective.side_effect = None
            transport.collective.return_value = result
            if succeeded:
                completed = verify(settings, journal, transport)
                assert completed is not None and completed.outcome == "complete"
                assert UpgradeEvidence.model_validate_json(completed.evidence).collective_verified
            elif stage == "candidate":
                with pytest.raises(UpgradeUnavailable):
                    verify(settings, journal, transport)
                assert journal.receipts.latest() == receipt
            else:
                completed = verify(settings, journal, transport)
                assert completed is not None and completed.outcome == "complete"
                assert not UpgradeEvidence.model_validate_json(
                    completed.evidence
                ).collective_verified
            assert journal.current() == intent
            assert transport.collective.call_args.kwargs["deadline_at"] == deadline
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


@pytest.mark.parametrize("fault", [None, "unpublished", "busy", "cancelled"])
def test_preparation_journals_stages_before_waiting_for_live_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from coire_node import upgrade_workflow
    from coire_node.upgrade_guard import NativeGuardJournal

    plan, source, candidate = native_plan(tmp_path)
    journal = NativeGuardJournal(
        tmp_path / "operation",
        plan,
        source,
        candidate,
        NodeAgentProcessIdentity(
            pid=123456, create_time_s=123.0, environment_sha256=source.runtime.environment_sha256
        ),
    )
    settings = Settings(
        node_name="coire-edge-a",
        node_state_dir=str(tmp_path / "state"),
        upgrade_bootstrap_root=str(tmp_path / "bootstrap"),
    )
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    if fault != "unpublished":
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
    PrivateUpgradeStore(tmp_path / "state/upgrades").write("maintenance.json", plan.maintenance)
    if fault == "cancelled":
        journal.root.write("cancel.json", NodeUpgradeCancel(generation=1))
    transport = Mock()
    transport.status.return_value = NodeUpgradeStatus(
        node="coire-edge-a",
        job_id=plan.operation.job_id,
        generation=1,
        release_sha256=plan.source.manifest_sha256,
        runtime_sha256=source.runtime.environment_sha256,
        environment_id=source.runtime.environment_id,
        bootstrap_ready=True,
        observed_at=datetime.now(UTC),
    )
    transport.inventory.return_value = NodeUpgradeInventory(
        job_id=plan.operation.job_id,
        generation=1,
        node="coire-edge-a",
        runtime_sha256=source.runtime.environment_sha256,
        observed_at=datetime.now(UTC),
        owners=()
        if fault == "busy"
        else tuple(
            UpgradeOwnerInventory(owner=name, status="complete") for name in REQUIRED_OWNERS
        ),
    )
    prepare = Mock(return_value=candidate)
    smoke = Mock(
        return_value=UpgradeEvidence(
            check="node",
            runtime_sha256=candidate.environment_sha256,
            model_id=plan.model_id,
            model_variant_id=plan.model_variant_id,
            model_manifest_sha256=plan.model_manifest_sha256,
            generated_tokens=10,
            output_sha256="a" * 64,
            cleanup_verified=True,
        )
    )
    monkeypatch.setattr(upgrade_workflow, "prepare_native_candidate", prepare)
    monkeypatch.setattr(upgrade_workflow, "run_guard_smoke", smoke)
    try:
        if fault == "unpublished":
            with pytest.raises(UpgradeConflict):
                upgrade_workflow.prepare_guard_step(settings, journal, transport)
        elif fault == "cancelled":
            assert upgrade_workflow.prepare_guard_step(settings, journal, transport) is None
        else:
            expected = [UpgradePhase.PREFLIGHT, UpgradePhase.FENCING, UpgradePhase.DRAINING]
            if fault != "busy":
                expected.extend([UpgradePhase.STAGED, UpgradePhase.SMOKE_BEFORE])
            for sequence, phase in enumerate(expected, 1):
                receipt = upgrade_workflow.prepare_guard_step(settings, journal, transport)
                assert receipt is not None and (receipt.sequence, receipt.phase) == (
                    sequence,
                    phase,
                )
            assert upgrade_workflow.prepare_guard_step(settings, journal, transport) is None
        if fault:
            prepare.assert_not_called()
            smoke.assert_not_called()
        else:
            prepare.assert_called_once()
            smoke.assert_called_once()
            assert journal.current().activation_started_at is None
            assert len(journal.receipts.page()) == 5
            # Preparation alone cannot select the candidate. Only a consumed,
            # sequence-bound Core grant can advance the independent owner.
            assert upgrade_workflow.activate_guard_step(settings, journal, transport) is None
            latest = journal.receipts.latest()
            now = datetime.now(UTC)
            grant = UpgradeActivationGrant(
                job_id=plan.operation.job_id,
                generation=1,
                boundary="runtime",
                grant_id=uuid4(),
                expires_at=now + timedelta(seconds=30),
            )
            command = NodeUpgradeCommand.model_validate(
                plan.model_dump(mode="json")
                | {
                    "activation_grant": grant,
                    "authorization_expires_at": grant.expires_at,
                    "activation_receipt": latest.model_copy(
                        update={
                            "sequence": latest.sequence + 1,
                            "phase": UpgradePhase.ACTIVATING,
                            "effect_id": "activation",
                            "outcome": "intent",
                            "evidence": "",
                            "occurred_at": now,
                        }
                    ),
                }
            )
            journal.root.write("command.json", command)

            def select(*args: object, **kwargs: object) -> object:
                assert callable(kwargs["refresh_inventory"])
                assert kwargs["refresh_inventory"]() == transport.inventory.return_value
                current = journal.current().model_copy(update={"phase": "candidate_selected"})
                journal.store.write("intent.json", current)
                return current

            activate = Mock(side_effect=select)
            monkeypatch.setattr(upgrade_workflow, "activate_native_candidate", activate)
            verifying = upgrade_workflow.activate_guard_step(settings, journal, transport)
            assert verifying is not None and verifying.phase is UpgradePhase.VERIFYING
            assert verifying.sequence == 7 and verifying.outcome == "intent"
            assert upgrade_workflow.activate_guard_step(settings, journal, transport) is None
            activate.assert_called_once()
            original_intent = journal.current()
            transport.status.side_effect = UpgradeUnavailable("replacement listener is starting")
            assert upgrade_workflow.advance_guard_step(settings, journal, transport) is None
            assert journal.current() == original_intent
            assert journal.current().recovery_started_at is None
            assert smoke.call_count == 1
            activate.assert_called_once()
            transport.status.side_effect = None
            # An old source response cannot satisfy post-switch serving.
            assert upgrade_workflow.verify_candidate_step(settings, journal, transport) is None
            assert smoke.call_count == 1
            transport.status.return_value = NodeUpgradeStatus(
                node="coire-edge-a",
                job_id=plan.operation.job_id,
                generation=1,
                release_sha256=plan.target.manifest_sha256,
                runtime_sha256=candidate.environment_sha256,
                environment_id=candidate.environment_id,
                bootstrap_ready=True,
                observed_at=datetime.now(UTC),
            )
            served = upgrade_workflow.verify_candidate_step(settings, journal, transport)
            assert served is not None and served.phase is UpgradePhase.VERIFYING
            assert served.outcome == "complete" and served.sequence == 8
            assert smoke.call_count == 2
            assert smoke.call_args.args[2].stage == "candidate-serving"
            assert smoke.call_args.args[2].deadline_at == journal.current().readiness_deadline_at
            assert upgrade_workflow.verify_candidate_step(settings, journal, transport) is None
            assert smoke.call_count == 2
            journal.root.write("cancel.json", NodeUpgradeCancel(generation=1))
            restoring = upgrade_workflow.advance_guard_step(settings, journal, transport)
            assert restoring is not None and restoring.phase is UpgradePhase.RESTORING
            assert journal.current().phase == "restoring"
            recovery_deadline = journal.current().recovery_deadline_at
            assert upgrade_workflow.activate_guard_step(settings, journal, transport) is None
            activate.assert_called_once()

            def restore(*args: object, **kwargs: object) -> object:
                assert callable(kwargs["refresh_candidate_inventory"])
                current = journal.current().model_copy(update={"phase": "source_selected"})
                journal.store.write("intent.json", current)
                return current

            restore_effect = Mock(side_effect=restore)
            monkeypatch.setattr(upgrade_workflow, "restore_native_source", restore_effect)
            source_intent = upgrade_workflow.restore_guard_step(settings, journal, transport)
            assert source_intent is not None
            assert source_intent.phase is UpgradePhase.VERIFYING_SOURCE
            assert journal.current().recovery_deadline_at == recovery_deadline
            assert upgrade_workflow.verify_source_step(settings, journal, transport) is None
            assert smoke.call_count == 2
            transport.status.return_value = transport.status.return_value.model_copy(
                update={
                    "release_sha256": plan.source.manifest_sha256,
                    "runtime_sha256": source.runtime.environment_sha256,
                    "environment_id": source.runtime.environment_id,
                }
            )
            smoke.return_value = smoke.return_value.model_copy(
                update={"runtime_sha256": source.runtime.environment_sha256}
            )
            source_complete = upgrade_workflow.verify_source_step(settings, journal, transport)
            assert source_complete is not None and source_complete.outcome == "complete"
            assert smoke.call_count == 3
            assert smoke.call_args.args[2].stage == "source-serving"
            deadline = journal.current().recovery_deadline_at
            # Receipt-to-intent reconciliation survives a recovered journal,
            # with no regenerated output and no renewed recovery timer.
            recovered = NativeGuardJournal(
                journal.root.root, plan, source, candidate, journal.current().source_process
            )
            assert upgrade_workflow.verify_source_step(settings, recovered, transport) is None
            assert recovered.current().phase == "source_verified"
            assert recovered.current().recovery_deadline_at == deadline
            assert smoke.call_count == 3
            restore_effect.assert_called_once()
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


@pytest.mark.parametrize("detection_delay", [0, 350])
def test_absent_candidate_listener_waits_only_its_original_readiness_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detection_delay: int
) -> None:
    from coire_node import upgrade_workflow
    from coire_node.upgrade_guard import NativeGuardJournal

    plan, source, candidate = native_plan(tmp_path)
    journal = NativeGuardJournal(
        tmp_path / "operation",
        plan,
        source,
        candidate,
        NodeAgentProcessIdentity(
            pid=123456, create_time_s=123.0, environment_sha256=source.runtime.environment_sha256
        ),
    )
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=120)
    intent = NativeRecoveryIntent.model_validate(
        journal.current().model_dump()
        | {
            "phase": "candidate_selected",
            "activation_started_at": now,
            "activation_grant_id": uuid4(),
            "readiness_deadline_at": deadline,
        }
    )
    journal.store.write("intent.json", intent)
    journal.receipts.append(
        UpgradeReceipt(
            job_id=plan.operation.job_id,
            generation=1,
            sequence=1,
            phase=UpgradePhase.VERIFYING,
            effect_id="serving",
            occurred_at=now,
            outcome="intent",
        )
    )
    settings = Settings(
        node_name="coire-edge-a", upgrade_bootstrap_root=str(tmp_path / "bootstrap")
    )
    PrivateUpgradeStore(tmp_path / "bootstrap").write(
        "claim.json",
        NativeUpgradeClaim(
            job_id=plan.operation.job_id,
            generation=1,
            node="coire-edge-a",
            plan_sha256=plan.authority_sha256(),
            updated_at=now,
        ),
    )
    transport = Mock()
    transport.status.side_effect = UpgradeUnavailable("replacement has no listener yet")
    clock = Mock(wraps=datetime)
    clock.now.return_value = deadline - timedelta(seconds=1)
    monkeypatch.setattr(upgrade_workflow, "datetime", clock)
    restore = Mock(return_value=None)
    monkeypatch.setattr(upgrade_workflow, "restore_guard_step", restore)
    monkeypatch.setattr(upgrade_workflow, "verify_source_step", Mock(return_value=None))
    try:
        assert upgrade_workflow.advance_guard_step(settings, journal, transport) is None
        assert journal.current() == intent
        restore.assert_not_called()
        clock.now.return_value = deadline + timedelta(seconds=detection_delay)
        upgrade_workflow.advance_guard_step(settings, journal, transport)
        recovered = journal.current()
        assert recovered.recovery_started_at == deadline
        assert recovered.recovery_deadline_at == deadline + timedelta(seconds=300)
        if detection_delay:
            assert recovered.phase == "recovery_required"
            restore.assert_not_called()
        else:
            assert recovered.phase == "restoring"
            restore.assert_called_once()
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


@pytest.mark.parametrize("expired", [True, False])
def test_recovery_deadline_closes_progress_without_releasing_unknown_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expired: bool
) -> None:
    from coire_core.models.upgrades import NativeRecoveryIntent, UpgradeReceipt
    from coire_node import upgrade_workflow
    from coire_node.upgrade_guard import NativeGuardJournal

    plan, source, candidate = native_plan(tmp_path)
    journal = NativeGuardJournal(
        tmp_path / "operation",
        plan,
        source,
        candidate,
        NodeAgentProcessIdentity(
            pid=123456, create_time_s=123.0, environment_sha256=source.runtime.environment_sha256
        ),
    )
    now = datetime.now(UTC)
    recovery_at = now - timedelta(seconds=400 if expired else 1)
    activation_at = recovery_at - timedelta(seconds=1)
    intent = NativeRecoveryIntent.model_validate(
        journal.current().model_dump(mode="json")
        | {
            "phase": "restoring",
            "activation_started_at": activation_at,
            "activation_grant_id": uuid4(),
            "readiness_deadline_at": activation_at + timedelta(seconds=120),
            "recovery_started_at": recovery_at,
            "recovery_deadline_at": recovery_at + timedelta(seconds=300),
        }
    )
    journal.store.write("intent.json", intent)
    journal.receipts.append(
        UpgradeReceipt(
            job_id=plan.operation.job_id,
            generation=1,
            sequence=1,
            phase=UpgradePhase.RESTORING,
            effect_id="restore",
            occurred_at=recovery_at,
            outcome="intent",
        )
    )
    settings = Settings(
        node_name="coire-edge-a",
        node_state_dir=str(tmp_path / "state"),
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
    restore = Mock()
    monkeypatch.setattr(upgrade_workflow, "restore_native_source", restore)
    try:
        if not expired:
            with pytest.raises(UpgradeConflict):
                upgrade_workflow.record_recovery_required(journal)
            assert journal.current() == intent
        else:
            receipt = upgrade_workflow.advance_guard_step(settings, journal, Mock())
            assert receipt is not None and receipt.phase is UpgradePhase.RECOVERY_REQUIRED
            assert receipt.outcome == "unknown" and receipt.restoration == "pending"
            assert receipt.evidence == "" and receipt.observed_sha256 is None
            assert upgrade_workflow.advance_guard_step(settings, journal, Mock()) is None
            assert journal.current().phase == "recovery_required"
            assert journal.current().recovery_deadline_at == intent.recovery_deadline_at
        restore.assert_not_called()
        assert state.read("maintenance.json", type(plan.maintenance)) == plan.maintenance
        assert root.read("claim.json", NativeUpgradeClaim).state == "active"
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
