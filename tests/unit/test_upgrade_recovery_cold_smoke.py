"""Separate Source smoke provenance, private revocation and evidence replay."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_cold_source import source_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdSmokeReceipt,
    UpgradeExpiredRecoveryColdSourceLaunch,
    UpgradeExpiredRecoveryColdSourceProcess,
)
from coire_core.models.upgrades import (
    NativeSmokeAttempt,
    NodeAgentProcessIdentity,
    UpgradeEvidence,
    UpgradeMaintenance,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_lease import bind_recovery_lease


@pytest.mark.parametrize(
    "fault", [None, "revoked", "foreign_registry", "missing_attempt", "lost_receipt"]
)
@pytest.mark.parametrize(
    "continuation",
    [False, True, "post_handback", "inherited"],
    ids=["cold", "continuation", "post_handback", "inherited"],
)
def test_source_smoke_separate_namespace_and_captured_evidence(
    tmp_path, monkeypatch, fault, continuation
):
    from coire_node import expired_recovery_cold_smoke as worker

    if continuation:
        from test_upgrade_recovery_cold_retained_source import continuation_fixture

        if continuation == "inherited":
            from test_upgrade_inherited_recovery_fence import inherited_continuation_fixture

            def continuation_fixture(path, patch, **_kw):
                return inherited_continuation_fixture(path, patch)

        selector, _, _, settings, claim, bootstrap, run, _, retained, control = (
            continuation_fixture(
                tmp_path, monkeypatch, post_handback=continuation == "post_handback"
            )
        )
        now = claim.claimed_at + timedelta(seconds=1)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=UTC):
                return now

        monkeypatch.setattr(__import__(__name__), "datetime", Clock)
        monkeypatch.setattr(worker, "datetime", Clock)
    else:
        selector, settings, claim, bootstrap, run, _, _, control = source_fixture(
            tmp_path, monkeypatch
        )
    control.verify_cold_recovery_smoke = Mock(side_effect=lambda smoke: smoke)
    monkeypatch.setattr(worker, "require_live_cold_source", lambda *_: None)
    plan = claim.intent.original_plan
    original_node = PrivateUpgradeStore(tmp_path / "state/upgrades")
    original_bytes = original_node.path("maintenance.json").read_bytes()
    probe_calls = []

    def probe(shadow, journal, command, *, authority):
        probe_calls.append(command)
        assert shadow.node_state_dir == str(run.root / "smoke-state")
        assert journal.store.root == run.root / "smoke-operation/guard"
        assert command.deadline_at == claim.intent.deadline_at
        if fault == "revoked":
            control.verify_cold_recovery_smoke.side_effect = UpgradeUnavailable(
                "fixture revoked during probe"
            )
        authority()
        evidence = UpgradeEvidence(
            check="node",
            runtime_sha256=claim.original_native.source.runtime.environment_sha256,
            model_id=plan.model_id,
            model_variant_id=plan.model_variant_id,
            model_manifest_sha256=plan.model_manifest_sha256,
            output_sha256="a" * 64,
            generated_tokens=10,
            cleanup_verified=True,
        )
        if fault != "missing_attempt":
            attempt = NativeSmokeAttempt(
                job_id=plan.operation.job_id,
                generation=plan.operation.generation,
                plan_sha256=plan.authority_sha256(),
                stage="source-serving",
                runtime_sha256=command.runtime_sha256,
                deadline_at=command.deadline_at,
                state="complete",
                process=NodeAgentProcessIdentity(
                    pid=98766,
                    create_time_s=datetime.now(UTC).timestamp(),
                    environment_sha256=command.runtime_sha256,
                ),
                cleanup_verified=True,
                evidence=evidence,
                updated_at=datetime.now(UTC),
            )
            journal.store.write("source-serving.json", attempt)
        return evidence

    monkeypatch.setattr(worker, "run_guard_smoke", probe)
    original_write = PrivateUpgradeStore.write
    lost = False

    def write(store, key, value, **kw):
        nonlocal lost
        result = original_write(store, key, value, **kw)
        if fault == "lost_receipt" and key == "cold.smoke.receipt.json" and not lost:
            lost = True
            raise UpgradeUnavailable("fixture lost acknowledgement")
        return result

    monkeypatch.setattr(PrivateUpgradeStore, "write", write)
    with (
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, claim.sha256(), descriptor),
    ):
        selected = selector.project_cold_source(settings, claim, control)
        launch = UpgradeExpiredRecoveryColdSourceLaunch(
            source=selected,
            attempt=0,
            started_at=datetime.now(UTC),
            retained_process=retained.process if continuation else None,
        )
        source = UpgradeExpiredRecoveryColdSourceProcess(
            launch=launch,
            process=retained.process
            if continuation
            else NodeAgentProcessIdentity(
                pid=98765,
                create_time_s=datetime.now(UTC).timestamp(),
                environment_sha256=claim.original_native.source.runtime.environment_sha256,
            ),
            observed_at=datetime.now(UTC),
        )
        if fault == "foreign_registry":
            control.verify_cold_recovery_smoke.side_effect = lambda smoke: smoke.model_copy(
                update={"captured_at": datetime.now(UTC) + timedelta(seconds=1)}
            )
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                worker.run_cold_source_smoke(settings, source, control)
        if fault in {None, "lost_receipt"}:
            receipt = worker.run_cold_source_smoke(settings, source, control)
            assert worker.run_cold_source_smoke(settings, source, control) == receipt
            assert (
                run.read("cold.smoke.receipt.json", UpgradeExpiredRecoveryColdSmokeReceipt)
                == receipt
            )
        elif fault == "revoked":
            shadow = PrivateUpgradeStore(run.root / "smoke-state/upgrades", create=False)
            assert shadow.read("maintenance.json", UpgradeMaintenance).state == "released"
            with pytest.raises(UpgradeUnavailable):
                worker.run_cold_source_smoke(settings, source, control)
    assert original_node.path("maintenance.json").read_bytes() == original_bytes
    assert len(probe_calls) == (0 if fault == "foreign_registry" else 1)
