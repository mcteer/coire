"""Fixed stop producer journals before command and preserves Source identity."""

import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_upgrade_expired_recovery_transfer import transfer_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryLegacyService,
    UpgradeExpiredRecoveryOwnerTransfer,
    UpgradeExpiredRecoveryTransferConsumption,
)
from coire_core.models.upgrades import NativeNodeProcess
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "failed",
    [
        False,
        True,
        "loaded",
        "alive",
        "source_changed",
        "lost_receipt",
        "cancel_after_initial_probe",
        "cancel_before_intent",
        "cancel_before_bootout",
        "elapsed_initial_probe",
        "elapsed_before_bootout",
        "elapsed_after_bootout",
    ],
)
def test_legacy_stop_journals_before_single_fixed_command(tmp_path, monkeypatch, failed):
    from coire_node import expired_recovery_stop as worker

    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    receipt = UpgradeExpiredRecoveryTransferConsumption(
        consumption_id=uuid4(),
        transfer=transfer,
        consumer=transfer.successor,
        consumed_at=transfer.issued_at,
    )
    settings = Settings(
        node_name="coire-edge-a", upgrade_bootstrap_root=str(tmp_path / "bootstrap")
    )
    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(bootstrap.root / "expired-recovery")
    run = PrivateUpgradeStore(runs.root / transfer.intent.recovery_id)
    run.write("consumption.json", receipt)
    service = UpgradeExpiredRecoveryLegacyService(
        target="system/com.coire.upgrade-guard",
        owner_uid=501,
        abandon_process_group=True,
        process=transfer.legacy_guard.process,
        observation_sha256="a" * 64,
        observed_at=datetime.now(UTC),
    )
    source = NativeNodeProcess(
        selected=transfer.legacy_guard.selected,
        process=transfer.legacy_guard.process.model_copy(
            update={"pid": transfer.legacy_guard.process.pid + 1000}
        ),
        recorded_at=datetime.now(UTC),
    )
    events = Mock()
    monkeypatch.setattr(worker, "stop_budget_events", events, raising=False)
    stopped = threading.Event()
    tick = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: tick[0])
    probes = [0]

    def verify(current):
        probes[0] += 1
        expiry_probe = {"elapsed_initial_probe": 1, "elapsed_before_bootout": 4}.get(failed)
        if failed == "elapsed_initial_probe":
            # No effect intent exists: a new invocation captures a new local cap.
            # Model persistent slow authority I/O on every attempt, not durable
            # monotonic recovery across invocations with frozen fixture UTC.
            tick[0] += 31.0
        elif expiry_probe is not None and probes[0] == expiry_probe:
            tick[0] = 31.0
        cancellation_probe = {
            "cancel_after_initial_probe": 1,
            "cancel_before_intent": 2,
            "cancel_before_bootout": 4,
        }.get(failed)
        if cancellation_probe is not None and probes[0] == cancellation_probe:
            stopped.set()
        return current

    control = SimpleNamespace(verify_recovery_consumption=Mock(side_effect=verify))
    monkeypatch.setattr(worker, "verify_local_transfer_preflight", Mock())
    monkeypatch.setattr(worker, "observe_legacy_guard_service", lambda *args: service)
    monkeypatch.setattr(
        worker,
        "fixed_launchctl_probe",
        lambda *args: lambda target: b"loaded" if failed == "loaded" else None,
    )
    monkeypatch.setattr(worker, "read_legacy_definition", lambda *args: "b" * 64)
    calls = []

    def source_probe(*args):
        if calls and failed == "source_changed":
            return source.model_copy(
                update={
                    "process": source.process.model_copy(update={"pid": source.process.pid + 1})
                }
            )
        return source

    monkeypatch.setattr(worker, "source_identity", source_probe)

    def retired(identity):
        if failed == "alive":
            raise UpgradeUnavailable("fixture old identity present")

    monkeypatch.setattr(worker, "verify_retired_identity", retired)
    if failed == "lost_receipt":
        write = PrivateUpgradeStore.write

        def interrupted(self, key, value, *, replace=True):
            write(self, key, value, replace=replace)
            if key == "legacy-stop.receipt.json":
                raise RuntimeError("fixture lost receipt response")

        monkeypatch.setattr(PrivateUpgradeStore, "write", interrupted)

    def command(argv, *, timeout):
        assert (run.root / "legacy-stop.intent.json").exists()
        calls.append(argv)
        if failed == "elapsed_after_bootout":
            tick[0] = 31.0
        return SimpleNamespace(returncode=1 if failed is True else 0)

    monkeypatch.setattr(worker, "capture_owned_command", command)
    if failed == "lost_receipt":
        with pytest.raises(RuntimeError, match="lost receipt response"):
            worker.stop_legacy_guard(
                settings, receipt, transfer.legacy_guard.bootstrap_runtime, control
            )
        assert (run.root / "legacy-stop.receipt.json").exists()
    elif failed:
        with pytest.raises(UpgradeUnavailable):
            worker.stop_legacy_guard(
                settings,
                receipt,
                transfer.legacy_guard.bootstrap_runtime,
                control,
                **({"stopped": stopped} if str(failed).startswith("cancel_") else {}),
            )
        if str(failed).startswith("cancel_") or str(failed).startswith("elapsed_"):
            assert run.path("legacy-stop.intent.json").exists() == (
                failed
                in {"cancel_before_bootout", "elapsed_before_bootout", "elapsed_after_bootout"}
            )
            assert not run.path("legacy-stop.receipt.json").exists()
        if str(failed).startswith("elapsed_"):
            events.add.assert_called_once_with(1, {"outcome": "refused"})
        else:
            events.add.assert_not_called()
    else:
        proof = worker.stop_legacy_guard(
            settings, receipt, transfer.legacy_guard.bootstrap_runtime, control
        )
        assert proof.preserved_source == source
        from coire_core.errors import UpgradeConflict
        from coire_node import expired_recovery_owner as owner

        monkeypatch.setattr(owner, "verify_local_successor_identity", lambda *args: None)
        monkeypatch.setattr(owner, "verify_retired_identity", lambda *args: None)
        monkeypatch.setattr(owner, "fixed_launchctl_probe", lambda *args: lambda target: None)
        monkeypatch.setattr(owner, "source_identity", lambda *args: source)
        monkeypatch.setattr(owner, "recovery_kernel_boot_identity", lambda *args: "test-kernel")
        support = transfer.intent.support_release.manifest
        runtime = transfer.legacy_guard.bootstrap_runtime.model_copy(
            update={
                "environment_id": support.environment_id,
                "environment_sha256": support.environment_sha256,
            }
        )
        with owner.recovery_owner_lease(settings, receipt, runtime, control) as claim:
            assert claim.process == receipt.consumer
            with pytest.raises(UpgradeConflict), bootstrap.ownership("recovery-transfer.lock"):
                pytest.fail("simultaneous owner acquired kernel lease")
        # Historical claim survives, but lease releases on exit; no readiness,
        # cleanup, original authority or serving status was published.
        with bootstrap.ownership("recovery-transfer.lock"):
            assert (run.root / "owner.claim.json").exists()
        with owner.recovery_owner_lease(settings, receipt, runtime, control) as replay:
            assert replay == claim
    if failed is False or failed == "lost_receipt":
        historical = worker.stop_legacy_guard(
            settings, receipt, transfer.legacy_guard.bootstrap_runtime, control
        )
        assert historical.effect.consumption_sha256 == receipt.sha256()
    else:
        with pytest.raises(UpgradeUnavailable):
            worker.stop_legacy_guard(
                settings,
                receipt,
                transfer.legacy_guard.bootstrap_runtime,
                control,
                **({"stopped": stopped} if str(failed).startswith("cancel_") else {}),
            )
    assert calls == (
        []
        if str(failed).startswith("cancel_")
        or failed in {"elapsed_initial_probe", "elapsed_before_bootout"}
        else [["/bin/launchctl", "bootout", service.target]]
    )
