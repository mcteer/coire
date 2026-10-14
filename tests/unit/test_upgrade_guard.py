"""Independent native guard persists fixed recovery authority before any selection effect."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from upgrade_fixtures import plan

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    NativeRuntimeSelection,
    NodeAgentProcessIdentity,
    NodeUpgradeCommand,
    PreparedNativeRuntime,
    UpgradeActivationGrant,
    UpgradeEvidence,
    UpgradeExecutionPlan,
    UpgradePhase,
    UpgradeReceipt,
)


def inputs() -> tuple[
    UpgradeExecutionPlan, NativeRuntimeSelection, PreparedNativeRuntime, NodeAgentProcessIdentity
]:
    value = plan()
    source = NativeRuntimeSelection(
        node="coire-edge-a",
        release_sha256=value.source.manifest_sha256,
        runtime=PreparedNativeRuntime(
            environment_id=value.source.manifest.environment_id,
            environment_sha256=value.source.manifest.environment_sha256,
            interpreter_id="source-python",
            interpreter_sha256="d" * 64,
        ),
        generation=0,
        selected_at=datetime.now(UTC),
    )
    candidate = PreparedNativeRuntime(
        environment_id=value.target.manifest.environment_id,
        environment_sha256=value.target.manifest.environment_sha256,
        interpreter_id="candidate-python",
        interpreter_sha256="e" * 64,
    )
    process = NodeAgentProcessIdentity(
        pid=123, create_time_s=100.0, environment_sha256=source.runtime.environment_sha256
    )
    return value, source, candidate, process


def test_guard_restart_retains_source_and_rejects_changed_authority(tmp_path: Path) -> None:
    from coire_node.upgrade_guard import NativeGuardJournal

    value, source, candidate, process = inputs()
    journal = NativeGuardJournal(tmp_path / "operation", value, source, candidate, process)
    assert journal.current().phase == "prepared"
    assert (
        NativeGuardJournal(tmp_path / "operation", value, source, candidate, process).current()
        == journal.current()
    )
    with pytest.raises(UpgradeConflict):
        NativeGuardJournal(
            tmp_path / "operation",
            value,
            source,
            candidate.model_copy(update={"environment_sha256": "f" * 64}),
            process,
        )
    journal.store.path("intent.json").write_text("corrupt")
    with pytest.raises(UpgradeUnavailable):
        journal.current()


@pytest.mark.parametrize("cleanup", [True, None, False])
@pytest.mark.parametrize("restoration_delay", [2, 121, 3600])
def test_native_activation_needs_consumed_receipt_and_real_cleanup_proof(
    tmp_path: Path,
    cleanup: bool | None,
    restoration_delay: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_node.upgrade_guard import NativeGuardJournal

    value, source, candidate, process = inputs()
    journal = NativeGuardJournal(tmp_path / "operation", value, source, candidate, process)
    now = datetime.now(UTC)
    evidence = UpgradeEvidence(
        check="node",
        runtime_sha256=candidate.environment_sha256,
        model_id=value.model_id,
        model_variant_id=value.model_variant_id,
        model_manifest_sha256=value.model_manifest_sha256,
        generated_tokens=10,
        output_sha256="f" * 64,
        cleanup_verified=cleanup,
    )
    smoke = UpgradeReceipt(
        job_id=value.operation.job_id,
        generation=1,
        sequence=1,
        phase=UpgradePhase.SMOKE_BEFORE,
        effect_id="candidate-smoke",
        occurred_at=now,
        outcome="complete",
        observed_sha256=value.operation.source_sha256,
        evidence=evidence.model_dump_json(),
    )
    grant = UpgradeActivationGrant(
        job_id=value.operation.job_id,
        generation=1,
        boundary="runtime",
        grant_id=uuid4(),
        expires_at=now + timedelta(seconds=30),
    )
    intent = smoke.model_copy(
        update={
            "sequence": 2,
            "phase": UpgradePhase.ACTIVATING,
            "effect_id": "runtime-activation",
            "outcome": "intent",
            "evidence": "",
        }
    )
    command = NodeUpgradeCommand.model_validate(
        value.model_dump(mode="json")
        | {
            "authorization_expires_at": grant.expires_at,
            "activation_grant": grant,
            "activation_receipt": intent,
        }
    )
    with pytest.raises(UpgradeConflict):
        journal.record_activation(command, now=now)
    journal.receipts.append(smoke)
    if cleanup is not True:
        with pytest.raises(UpgradeConflict):
            journal.record_activation(command, now=now)
        assert journal.current().phase == "prepared"
        assert not journal.store.path("activation.json").exists()
        return
    append = journal.receipts.append
    failed = False

    def interrupted(receipt: UpgradeReceipt, **kwargs: object) -> bool:
        nonlocal failed
        if not failed:
            failed = True
            raise UpgradeUnavailable("simulated receipt fsync failure")
        return append(receipt)

    monkeypatch.setattr(journal.receipts, "append", interrupted)
    with pytest.raises(UpgradeUnavailable):
        journal.record_activation(command, now=now)
    assert journal.current().phase == "activation_intent"
    activated = journal.record_activation(command, now=now + timedelta(seconds=1))
    assert activated.phase == "activation_intent"
    assert activated.readiness_deadline_at == now + timedelta(
        seconds=value.operation.timing.readiness_s
    )
    assert journal.receipts.latest() == intent
    assert journal.record_activation(command, now=now + timedelta(seconds=1)) == activated
    assert journal.current().readiness_deadline_at == activated.readiness_deadline_at
    recovery = journal.begin_restoration(now=now + timedelta(seconds=restoration_delay))
    assert recovery.phase == "restoring"
    assert recovery.recovery_deadline_at == now + timedelta(
        seconds=min(restoration_delay, value.operation.timing.readiness_s)
        + value.operation.timing.recovery_s
    )
    assert journal.begin_restoration(now=now + timedelta(hours=1)) == recovery


@pytest.mark.parametrize(
    "fault", ["none", "reused", "foreign-executable", "foreign-command", "unreadable"]
)
def test_node_process_identity_never_signals_recycled_or_unrelated_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    import psutil

    from coire_node.upgrade_guard import stop_owned_node_process

    _, _, _, identity = inputs()
    executable = tmp_path / "python"
    executable.write_bytes(b"node-runtime")
    process = Mock()
    process.create_time.return_value = 200.0 if fault == "reused" else identity.create_time_s
    process.is_running.return_value = True
    process.status.return_value = "running"
    process.uids.return_value = SimpleNamespace(real=os.getuid(), effective=os.getuid())
    process.exe.return_value = (
        str(tmp_path / "foreign") if fault == "foreign-executable" else str(executable)
    )
    process.cmdline.return_value = [
        str(executable),
        "-m",
        "coire_ops" if fault == "foreign-command" else "coire_node",
    ]
    if fault == "unreadable":
        process.exe.side_effect = psutil.AccessDenied(identity.pid)
    process.terminate.side_effect = lambda: setattr(process.is_running, "return_value", False)
    monkeypatch.setattr(psutil, "Process", lambda _: process)
    if fault in {"foreign-executable", "foreign-command", "unreadable"}:
        with pytest.raises(UpgradeUnavailable):
            stop_owned_node_process(identity, executable, datetime.now(UTC) + timedelta(seconds=1))
    else:
        assert stop_owned_node_process(
            identity, executable, datetime.now(UTC) + timedelta(seconds=1)
        )
    if fault == "none":
        process.terminate.assert_called_once()
    else:
        process.terminate.assert_not_called()


@pytest.mark.parametrize("with_abort", [False, True])
def test_expired_consumed_grant_only_captures_original_recovery_budget(
    tmp_path: Path, with_abort: bool
) -> None:
    from coire_core.models.upgrades import NativeAbortIntent
    from coire_node.upgrade_guard import NativeGuardJournal

    value, source, candidate, process = inputs()
    journal = NativeGuardJournal(tmp_path / "operation", value, source, candidate, process)
    consumed_at = datetime.now(UTC) - timedelta(seconds=100)
    smoke = UpgradeReceipt(
        job_id=value.operation.job_id,
        generation=1,
        sequence=1,
        phase=UpgradePhase.SMOKE_BEFORE,
        effect_id="candidate-smoke",
        occurred_at=consumed_at - timedelta(seconds=1),
        outcome="complete",
        observed_sha256=value.operation.source_sha256,
        evidence=UpgradeEvidence(
            check="node",
            runtime_sha256=candidate.environment_sha256,
            model_id=value.model_id,
            model_variant_id=value.model_variant_id,
            model_manifest_sha256=value.model_manifest_sha256,
            generated_tokens=10,
            output_sha256="f" * 64,
            cleanup_verified=True,
        ).model_dump_json(),
    )
    grant = UpgradeActivationGrant(
        job_id=value.operation.job_id,
        generation=1,
        boundary="runtime",
        grant_id=uuid4(),
        expires_at=consumed_at + timedelta(seconds=30),
    )
    consumed = smoke.model_copy(
        update={
            "sequence": 2,
            "phase": UpgradePhase.ACTIVATING,
            "effect_id": "runtime-activation",
            "outcome": "intent",
            "occurred_at": consumed_at,
            "evidence": "",
        }
    )
    command = NodeUpgradeCommand.model_validate(
        value.model_dump(mode="json")
        | {
            "authorization_expires_at": grant.expires_at,
            "activation_grant": grant,
            "activation_receipt": consumed,
        }
    )
    journal.receipts.append(smoke)
    recovery_at = grant.expires_at
    if with_abort:
        recovery_at = consumed_at + timedelta(seconds=10)
        journal.store.write(
            "abort.json",
            NativeAbortIntent(
                job_id=value.operation.job_id,
                generation=1,
                plan_sha256=value.authority_sha256(),
                reason="cancelled",
                captured_at=recovery_at,
                deadline_at=recovery_at + timedelta(seconds=value.operation.timing.recovery_s),
            ),
        )
    with pytest.raises(UpgradeConflict):
        journal.record_activation(command)
    assert journal.current().phase == "prepared"
    captured = journal.record_activation(command, recovery_only=True)
    assert captured.phase == "restoring"
    assert captured.recovery_started_at == recovery_at
    assert captured.recovery_deadline_at == recovery_at + timedelta(
        seconds=value.operation.timing.recovery_s
    )
    restarted = NativeGuardJournal(tmp_path / "operation", value, source, candidate, process)
    assert (
        restarted.record_activation(
            command, now=datetime.now(UTC) + timedelta(hours=1), recovery_only=True
        )
        == captured
    )
    assert restarted.begin_restoration(now=datetime.now(UTC) + timedelta(hours=1)) == captured
