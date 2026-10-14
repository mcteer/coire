"""Distinct pair dispatch and all durable retry boundaries; worker facts synthetic."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_upgrade_recovery_cold_archive import archive_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.sharding import LinkObservation, ProbeOutcome, ProbeTransport
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdPairQualification,
    UpgradeExpiredRecoveryColdSmokeCommand,
    UpgradeExpiredRecoveryColdSmokeReceipt,
    UpgradeExpiredRecoveryColdSourceLaunch,
    UpgradeExpiredRecoveryColdSourceProcess,
)
from coire_core.models.upgrades import (
    NativeSmokeAttempt,
    NativeSmokeCommand,
    NodeAgentProcessIdentity,
    UpgradeEvidence,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_lease import bind_recovery_lease


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "failed",
        "foreign_runtime",
        "lost_command",
        "lost_result",
        "lost_qualification",
        "busy_peer",
        "revoked",
        "archive_changed",
    ],
)
@pytest.mark.parametrize(
    "continuation",
    [False, True, "post_handback", "inherited"],
    ids=["cold", "continuation", "post_handback", "inherited"],
)
def test_pair_new_commands_retained_results_and_no_interrupted_relaunch(
    tmp_path, monkeypatch, fault, continuation
):
    from coire_node import expired_recovery_cold_pair as worker

    if continuation:
        from test_upgrade_recovery_cold_retained_source import continuation_fixture

        if continuation == "inherited":
            from test_upgrade_inherited_recovery_fence import inherited_continuation_fixture

            def continuation_fixture(path, patch, **_kw):
                return inherited_continuation_fixture(path, patch)

        from coire_node import expired_recovery_cold_archive as archiver

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

        for module in (__import__(__name__), worker, archiver):
            monkeypatch.setattr(module, "datetime", Clock)
        for name in ("local_inventory", "peer_inventory"):
            inventory = getattr(control, name)()
            setattr(
                control,
                name,
                Mock(return_value=inventory.model_copy(update={"observed_at": now})),
            )
    else:
        archiver, selector, settings, claim, bootstrap, run, _, _, _, control = archive_fixture(
            tmp_path, monkeypatch
        )
    hostfile = tmp_path / "hostfile.json"
    hostfile.write_bytes(b"sealed fixture hostfile")
    hostfile.chmod(0o600)
    settings = settings.model_copy(update={"sharding_jaccl_hostfile": str(hostfile)})
    monkeypatch.setattr(worker, "require_live_cold_source", lambda *_: None)
    control.verify_cold_recovery_smoke = Mock(side_effect=lambda command: command)
    original_write = PrivateUpgradeStore.write
    lost = False

    def write(store, key, value, **kw):
        nonlocal lost
        result = original_write(store, key, value, **kw)
        expected = {
            "lost_command": "cold.pair.0.command.json",
            "lost_result": "cold.pair.0.result.json",
            "lost_qualification": "cold.pair.qualification.json",
        }.get(fault)
        if key == expected and not lost:
            lost = True
            raise UpgradeUnavailable("fixture lost durable pair response")
        return result

    monkeypatch.setattr(PrivateUpgradeStore, "write", write)
    calls = []

    def dispatch(command, *, deadline_at, authority):
        authority()
        calls.append(command)
        assert deadline_at == claim.intent.deadline_at
        return LinkObservation(
            id=uuid4(),
            node_a="coire-edge-a",
            node_b="coire-edge-b",
            transport=ProbeTransport.JACCL,
            outcome=ProbeOutcome.FAILED if fault == "failed" else ProbeOutcome.SUCCEEDED,
            bandwidth_bytes_per_second=None if fault == "failed" else 1024,
            latency_ms=None if fault == "failed" else 1.0,
            os_version_a="synthetic",
            os_version_b="synthetic",
            engine_version="synthetic",
            runtime_sha256_a="f" * 64
            if fault == "foreign_runtime"
            else claim.original_native.source.runtime.environment_sha256,
            runtime_sha256_b=claim.intent.original_plan.peer_runtime_sha256,
            observed_at=datetime.now(UTC),
        )

    runner = Mock()
    runner.run.side_effect = dispatch
    with (
        bootstrap.ownership("lifetime.lock"),
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
        archive = archiver.archive_cold_collectives(settings, source, control)
        plan = claim.intent.original_plan
        command = UpgradeExpiredRecoveryColdSmokeCommand(
            source=source,
            command=NativeSmokeCommand(
                plan=plan,
                runtime_sha256=claim.original_native.source.runtime.environment_sha256,
                stage="source-serving",
                deadline_at=claim.intent.deadline_at,
            ),
            captured_at=datetime.now(UTC),
        )
        smoke = UpgradeExpiredRecoveryColdSmokeReceipt(
            smoke=command,
            attempt=NativeSmokeAttempt(
                job_id=plan.operation.job_id,
                generation=plan.operation.generation,
                plan_sha256=plan.authority_sha256(),
                stage="source-serving",
                runtime_sha256=command.command.runtime_sha256,
                state="complete",
                process=source.process,
                evidence=UpgradeEvidence(
                    check="node",
                    runtime_sha256=command.command.runtime_sha256,
                    model_id=plan.model_id,
                    model_variant_id=plan.model_variant_id,
                    model_manifest_sha256=plan.model_manifest_sha256,
                    output_sha256="a" * 64,
                    generated_tokens=10,
                    cleanup_verified=True,
                ),
                cleanup_verified=True,
                deadline_at=claim.intent.deadline_at,
                updated_at=datetime.now(UTC),
            ),
            observed_at=datetime.now(UTC),
        )
        run.write("cold.smoke.receipt.json", smoke)
        for name, snapshot in (
            ("local_inventory", archive.archive.local),
            ("peer_inventory", archive.archive.peer),
        ):

            def inventory(*_args, _snapshot=snapshot, **_kw):
                owners = tuple(
                    owner.model_copy(update={"entries": ()}) for owner in _snapshot.owners
                )
                if fault == "busy_peer" and _snapshot.node == "coire-edge-b":
                    owners = (owners[0].model_copy(update={"status": "unknown"}), *owners[1:])
                return _snapshot.model_copy(
                    update={"owners": owners, "observed_at": datetime.now(UTC)}
                )

            setattr(control, name, Mock(side_effect=inventory))
        if fault == "revoked":
            control.verify_cold_recovery_smoke.side_effect = UpgradeUnavailable("fixture revoked")
        if fault == "archive_changed":
            archived = (
                run.root
                / "collective-history"
                / claim.original_claim.job_id
                / "candidate/0/scope.json"
            )
            archived.write_bytes(archived.read_bytes() + b" ")
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                worker.qualify_cold_pair(settings, smoke, archive, control, runner)
        if fault in {None, "lost_result", "lost_qualification"}:
            proof = worker.qualify_cold_pair(settings, smoke, archive, control, runner)
            assert worker.qualify_cold_pair(settings, smoke, archive, control, runner) == proof
            assert (
                run.read(
                    "cold.pair.qualification.json", UpgradeExpiredRecoveryColdPairQualification
                )
                == proof
            )
            assert len(calls) == 3 and len({command.command_id for command in calls}) == 3
            assert all(
                command.command_id != archive.archive.scopes[0].command_id for command in calls
            )
        else:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                worker.qualify_cold_pair(settings, smoke, archive, control, runner)
            assert len(calls) == (1 if fault in {"failed", "foreign_runtime"} else 0)
    assert not (tmp_path / "state/upgrades/collectives" / claim.original_claim.job_id).exists()
