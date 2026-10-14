"""Real writer transfer and durable Source handback boundaries; native facts stubbed."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_cold_archive import archive_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    RECOVERY_PRESERVATION_TABLES,
    UpgradeExpiredRecoveryColdHandbackReceipt,
    UpgradeExpiredRecoveryColdSmokeCommand,
    UpgradeExpiredRecoveryColdSmokeReceipt,
    UpgradeExpiredRecoveryColdSourceLaunch,
    UpgradeExpiredRecoveryColdSourceProcess,
    UpgradeExpiredRecoveryColdVerification,
    UpgradeExpiredRecoveryColdVerificationRequest,
    UpgradeExpiredRecoveryDataSnapshot,
    UpgradeExpiredRecoveryNativeDataSnapshot,
)
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    NativeSmokeAttempt,
    NativeSmokeCommand,
    NodeAgentProcessIdentity,
    UpgradeEvidence,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_lease import bind_recovery_lease


@pytest.mark.parametrize(
    "interruption",
    [None, "intent", "selection", "accepted", "claim", "runtime", "helper", "receipt"],
)
def test_source_handback_exact_preimages_and_continuous_recovery_lock(
    tmp_path, monkeypatch, interruption
):
    perform_source_handback_fixture(tmp_path, monkeypatch, interruption=interruption)


def perform_source_handback_fixture(
    tmp_path,
    monkeypatch,
    *,
    interruption=None,
    callback=None,
    supervision="launchd",
    continuation=False,
    post_handback=False,
    inherited=False,
):
    monkeypatch.setenv("UPGRADE_RECOVERY_CORE_API_PORT", "48519")
    monkeypatch.setenv("CORE_API_PORT", "39300")
    from coire_node import expired_recovery_cold_handback as worker

    if continuation:
        from test_upgrade_recovery_cold_retained_source import continuation_fixture

        from coire_node import expired_recovery_cold_archive as archiver

        if inherited:
            from test_upgrade_inherited_recovery_fence import inherited_continuation_fixture

            prepared = inherited_continuation_fixture(tmp_path, monkeypatch)
        else:
            prepared = continuation_fixture(tmp_path, monkeypatch, post_handback=post_handback)
        selector, _, _, settings, owner, bootstrap, run, _, retained, control = prepared
        now = owner.claimed_at + timedelta(seconds=1)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=UTC):
                return now

        monkeypatch.setattr(__import__(__name__), "datetime", Clock)
        monkeypatch.setattr(worker, "datetime", Clock)
        monkeypatch.setattr(archiver, "datetime", Clock)
        for name in ("local_inventory", "peer_inventory"):
            inventory = getattr(control, name)()
            setattr(
                control,
                name,
                Mock(return_value=inventory.model_copy(update={"observed_at": now})),
            )
        supervision = "fixture"
    else:
        archiver, selector, settings, owner, bootstrap, run, _, _, _, control = archive_fixture(
            tmp_path, monkeypatch, supervision=supervision
        )
    helper = [None]
    source_writer = []
    monkeypatch.setattr(worker, "verify_passive_support_identity", lambda *_: owner.process)
    monkeypatch.setattr(
        worker, "recovery_kernel_boot_identity", lambda *_: owner.kernel_boot_identity
    )
    retired = []
    monkeypatch.setattr(worker, "verify_retired_identity", lambda value: retired.append(value))
    monkeypatch.setattr(worker, "require_live_cold_source", lambda *_: None)
    monkeypatch.setattr(worker, "observe_source_helper", lambda *_: helper[0])
    control.verify_cold_recovery_verification = Mock(side_effect=lambda proof: proof)
    control.accept_cold_recovery_handback = Mock(side_effect=lambda intent: intent)
    control.verify_cold_recovery_handback = Mock(side_effect=lambda intent: intent)
    original_write = PrivateUpgradeStore.write
    interrupted = False

    def write(store, key, value, **kw):
        nonlocal interrupted
        result = original_write(store, key, value, **kw)
        expected = {
            "intent": "cold.handback.intent.json",
            "selection": "selection.json",
            "accepted": "accepted.json",
            "claim": "claim.json",
            "runtime": "runtime.json",
            "helper": "cold.handback.helper.json",
            "receipt": "cold.handback.receipt.json",
        }.get(interruption)
        if key == expected and not interrupted:
            interrupted = True
            raise UpgradeUnavailable("fixture lost handback durable response")
        return result

    calls = []
    source = [None]

    def spawn(argv, **kw):
        calls.append(argv)
        assert argv == [
            str(tmp_path / "graphs/envs/source/bin/python3"),
            "-I",
            "-B",
            "-m",
            "coire_node.upgrade_guard",
        ]
        assert kw["env"]["UPGRADES_ENABLED"] == "true"
        assert "UPGRADE_RECOVERY_ID" not in kw["env"]
        assert "UPGRADE_RECOVERY_CORE_API_PORT" not in kw["env"]
        assert kw["env"]["CORE_API_PORT"] == "39300"
        assert run.path("cold.handback.launch.json").exists()
        # Actual second descriptor acquisition proves the parent released only
        # the writer lock. Recovery exclusion must remain continuously held.
        lease = bootstrap.ownership("lifetime.lock", wait_s=0)
        source_writer.append(lease)
        lease.__enter__()
        with (
            pytest.raises(UpgradeConflict),
            bootstrap.ownership("recovery-transfer.lock", wait_s=0),
        ):
            pass
        helper[0] = NodeAgentProcessIdentity(
            pid=98766,
            create_time_s=datetime.now(UTC).timestamp(),
            environment_sha256=owner.original_native.source.runtime.environment_sha256,
        )
        selected = source[0].launch.source.projection.source_selection
        bootstrap.write(
            "status.json",
            NativeGuardStatus(
                node=selected.node,
                bootstrap_runtime=selected.runtime,
                process=helper[0],
                selected=selected,
                ready=True,
                observed_at=datetime.now(UTC),
            ),
        )
        return SimpleNamespace(pid=98766)

    monkeypatch.setattr(worker.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        worker.psutil,
        "Process",
        lambda _: SimpleNamespace(create_time=lambda: helper[0].create_time_s),
    )
    try:
        records = PrivateUpgradeStore(bootstrap.root.parent / "fixture-records")
        with (
            (
                records.ownership("fixture-supervisor.lock")
                if supervision == "fixture"
                else nullcontext()
            ) as supervision_descriptor,
            bootstrap.ownership("lifetime.lock") as lifetime,
            bootstrap.ownership("recovery-transfer.lock") as recovery,
            bind_recovery_lease(
                bootstrap,
                owner.sha256(),
                recovery,
                lifetime_descriptor=lifetime,
                supervision_descriptor=supervision_descriptor,
            ),
        ):
            selected = selector.project_cold_source(settings, owner, control)
            launch = UpgradeExpiredRecoveryColdSourceLaunch(
                source=selected,
                attempt=0,
                started_at=datetime.now(UTC),
                retained_process=retained.process if continuation else None,
            )
            source[0] = UpgradeExpiredRecoveryColdSourceProcess(
                launch=launch,
                process=retained.process
                if continuation
                else NodeAgentProcessIdentity(
                    pid=98765,
                    create_time_s=datetime.now(UTC).timestamp(),
                    environment_sha256=selected.projection.source_selection.runtime.environment_sha256,
                ),
                observed_at=datetime.now(UTC),
            )
            archive = archiver.archive_cold_collectives(settings, source[0], control)
            plan = owner.intent.original_plan
            smoke_command = UpgradeExpiredRecoveryColdSmokeCommand(
                source=source[0],
                command=NativeSmokeCommand(
                    plan=plan,
                    stage="source-serving",
                    runtime_sha256=owner.original_native.source.runtime.environment_sha256,
                    deadline_at=owner.intent.deadline_at,
                ),
                captured_at=datetime.now(UTC),
            )
            smoke = UpgradeExpiredRecoveryColdSmokeReceipt(
                smoke=smoke_command,
                attempt=NativeSmokeAttempt(
                    job_id=plan.operation.job_id,
                    generation=plan.operation.generation,
                    plan_sha256=plan.authority_sha256(),
                    stage="source-serving",
                    runtime_sha256=smoke_command.command.runtime_sha256,
                    state="complete",
                    process=source[0].process,
                    evidence=UpgradeEvidence(
                        check="node",
                        runtime_sha256=smoke_command.command.runtime_sha256,
                        model_id=plan.model_id,
                        model_variant_id=plan.model_variant_id,
                        model_manifest_sha256=plan.model_manifest_sha256,
                        generated_tokens=10,
                        output_sha256="a" * 64,
                        cleanup_verified=True,
                    ),
                    cleanup_verified=True,
                    deadline_at=owner.intent.deadline_at,
                    updated_at=datetime.now(UTC),
                ),
                observed_at=datetime.now(UTC),
            )
            native_before = UpgradeExpiredRecoveryNativeDataSnapshot(
                original_job_id=plan.operation.job_id,
                original_generation=plan.operation.generation,
                source_sha256=plan.source.manifest_sha256,
                node=owner.original_claim.node,
                root_sha256s=dict.fromkeys(("datasets", "artifacts", "components"), "a" * 64),
                file_counts=dict.fromkeys(("datasets", "artifacts", "components"), 0),
                total_bytes=0,
                observed_at=owner.intent.accepted_at,
            )
            data_before = UpgradeExpiredRecoveryDataSnapshot(
                recovery_id=owner.intent.recovery_id,
                original_job_id=plan.operation.job_id,
                original_generation=plan.operation.generation,
                source_sha256=plan.source.manifest_sha256,
                table_sha256s=dict.fromkeys(RECOVERY_PRESERVATION_TABLES, "b" * 64),
                row_counts=dict.fromkeys(RECOVERY_PRESERVATION_TABLES, 0),
                observed_at=owner.intent.accepted_at,
            )
            local = archive.archive.local.model_copy(
                update={
                    "owners": tuple(
                        o.model_copy(update={"entries": ()}) for o in archive.archive.local.owners
                    ),
                    "observed_at": datetime.now(UTC),
                }
            )
            proof = UpgradeExpiredRecoveryColdVerification(
                request=UpgradeExpiredRecoveryColdVerificationRequest(
                    smoke=smoke,
                    archive=archive,
                    native_after=native_before.model_copy(
                        update={"observed_at": datetime.now(UTC)}
                    ),
                ),
                data_before=data_before,
                data_after=data_before.model_copy(update={"observed_at": datetime.now(UTC)}),
                native_before=native_before,
                local=local,
                peer=archive.archive.peer.model_copy(update={"observed_at": datetime.now(UTC)}),
                verified_at=datetime.now(UTC),
            )
            run.write("cold.verification.json", proof)
            bootstrap.write("selection.json", owner.original_native.source)
            operation = PrivateUpgradeStore(
                bootstrap.root / "operations" / plan.operation.job_id, create=False
            )
            operation.write("accepted.json", owner.original_claim)
            node = PrivateUpgradeStore(tmp_path / "state/upgrades", create=False)
            node.write(
                "node-process.json",
                NativeNodeProcess(
                    selected=selected.projection.source_selection,
                    process=source[0].process,
                    recorded_at=datetime.now(UTC),
                ),
            )
            monkeypatch.setattr(PrivateUpgradeStore, "write", write)
            if (post_handback or inherited) and interruption in {"accepted", "claim"}:
                # Released authority is deliberately never rewritten. Exercise
                # loss at its read/reconciliation boundary instead.
                original_read = PrivateUpgradeStore.read

                def read(store, key, model):
                    nonlocal interrupted
                    value = original_read(store, key, model)
                    if (
                        key == {"accepted": "accepted.json", "claim": "claim.json"}[interruption]
                        and not interrupted
                    ):
                        interrupted = True
                        raise UpgradeUnavailable("fixture lost handback retained read response")
                    return value

                monkeypatch.setattr(PrivateUpgradeStore, "read", read)
            if interruption:
                with pytest.raises(UpgradeUnavailable, match="lost handback"):
                    worker.handback_cold_source(settings, proof, control)
            receipt = worker.handback_cold_source(settings, proof, control)
            assert worker.handback_cold_source(settings, proof, control) == receipt
            if post_handback or inherited:
                from coire_core.models.upgrades import NativeUpgradeClaim

                assert bootstrap.read("claim.json", NativeUpgradeClaim) == owner.original_claim
                assert operation.read("accepted.json", NativeUpgradeClaim) == owner.original_claim
            assert (
                receipt.status.ready
                and receipt.status.bootstrap_runtime == owner.original_native.source.runtime
            )
            assert (
                run.read("cold.handback.receipt.json", UpgradeExpiredRecoveryColdHandbackReceipt)
                == receipt
            )
            with pytest.raises(UpgradeConflict), bootstrap.ownership("lifetime.lock", wait_s=0):
                pass
            if callback is not None:
                callback(settings, owner, bootstrap, run, receipt, control)
        assert len(calls) == 1
        if continuation:
            assert owner.last_node.process == retained.process
            assert retained.process not in retired
            assert owner.previous_guard.process in retired
            assert owner.original_native.source_process in retired
    finally:
        for lease in source_writer:
            lease.__exit__(None, None, None)


@pytest.mark.parametrize(
    "interruption",
    [None, "intent", "selection", "accepted", "claim", "runtime", "helper", "receipt"],
)
@pytest.mark.parametrize("post_handback", [False, True])
def test_continuation_handback_keeps_recorded_source_alive_at_every_boundary(
    tmp_path, monkeypatch, interruption, post_handback
):
    perform_source_handback_fixture(
        tmp_path,
        monkeypatch,
        interruption=interruption,
        continuation=True,
        post_handback=post_handback,
    )
