"""Real private locks/immutable journals; Darwin runtime/process facts are fixtures."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from test_upgrade_recovery_cold_archive import archive_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    RECOVERY_PRESERVATION_TABLES,
    UpgradeExpiredRecoveryColdSourceLaunch,
    UpgradeExpiredRecoveryColdSourceProcess,
    UpgradeExpiredRecoveryDataSnapshot,
    UpgradeExpiredRecoveryNativeDataSnapshot,
)
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationContext,
    UpgradeColdContinuationObserverInput,
    UpgradeColdContinuationRead,
)
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    NodeAgentProcessIdentity,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_lease import bind_recovery_lease


def observer_fixture(tmp_path, monkeypatch):
    from coire_node import expired_recovery_continuation_observer as worker

    _archive, selector, settings, owner, bootstrap, run, current, _history, _slot, control = (
        archive_fixture(tmp_path, monkeypatch, supervision="fixture")
    )
    with (
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, owner.sha256(), descriptor),
    ):
        receipt = selector.project_cold_source(settings, owner, control)
    stamp = receipt.observed_at + timedelta(milliseconds=1)
    launch = UpgradeExpiredRecoveryColdSourceLaunch(source=receipt, attempt=0, started_at=stamp)
    source = UpgradeExpiredRecoveryColdSourceProcess(
        launch=launch,
        process=NodeAgentProcessIdentity(
            pid=234567,
            create_time_s=stamp.timestamp(),
            environment_sha256=owner.original_native.source.runtime.environment_sha256,
        ),
        observed_at=stamp,
    )
    run.write("cold.source.process.0.json", source)
    PrivateUpgradeStore(bootstrap.root.parent / "fixture-records")
    bootstrap.write(
        "status.json",
        owner.previous_guard.model_copy(
            update={
                "process": owner.process,
                "selected": receipt.projection.source_selection,
                "bootstrap_runtime": owner.runtime,
            }
        ),
    )
    PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades").write(
        "node-process.json",
        NativeNodeProcess(
            selected=receipt.projection.source_selection, process=source.process, recorded_at=stamp
        ),
    )
    PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades").write(
        "maintenance.json",
        owner.intent.original_plan.maintenance.model_copy(update={"state": "active"}),
    )
    before = UpgradeExpiredRecoveryDataSnapshot(
        recovery_id=owner.intent.recovery_id,
        original_job_id=owner.original_claim.job_id,
        original_generation=owner.original_claim.generation,
        source_sha256=owner.intent.request.expected_source_sha256,
        table_sha256s=dict.fromkeys(RECOVERY_PRESERVATION_TABLES, "a" * 64),
        row_counts=dict.fromkeys(RECOVERY_PRESERVATION_TABLES, 0),
        observed_at=owner.intent.accepted_at,
    )
    native = UpgradeExpiredRecoveryNativeDataSnapshot(
        original_job_id=owner.original_claim.job_id,
        original_generation=owner.original_claim.generation,
        source_sha256=owner.intent.request.expected_source_sha256,
        node=settings.node_name,
        root_sha256s=dict.fromkeys(("datasets", "artifacts", "components"), "b" * 64),
        file_counts=dict.fromkeys(("datasets", "artifacts", "components"), 0),
        total_bytes=0,
        observed_at=owner.intent.accepted_at,
    )
    now = owner.intent.deadline_at + timedelta(seconds=1)
    context = UpgradeColdContinuationContext(
        intent=owner.intent,
        owner=owner,
        data_baseline=before,
        native_baseline=native,
        current_data=before.model_copy(update={"observed_at": now}),
        observed_at=now,
    )
    runtime = owner.runtime
    python = tmp_path / "graphs/envs/target/bin/python3"
    monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(
        worker,
        "sys",
        SimpleNamespace(
            platform="darwin", prefix=str(python.parent.parent), executable=str(python)
        ),
    )
    monkeypatch.setattr(worker.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(worker.platform, "node", lambda: "coire-edge-a")
    source_python = tmp_path / "graphs/envs/source/bin/python3"
    monkeypatch.setattr(
        worker,
        "verify_prepared_runtime",
        lambda _env, _interp, rt: (
            source_python if rt == owner.original_native.source.runtime else python
        ),
    )
    monkeypatch.setattr(worker, "_owned_native_process", lambda *_: object())
    child = SimpleNamespace(cmdline=lambda: [str(source_python), "-I", "-B", "-m", "coire_node"])
    monkeypatch.setattr(worker, "_owned_node_process", lambda *_: child)
    monkeypatch.setattr(worker, "verify_retired_identity", lambda *_: None)
    monkeypatch.setattr(worker, "observe_fixture_supervisor", lambda *_: owner.retired_supervisor)
    monkeypatch.setattr(
        worker, "recovery_kernel_boot_identity", lambda *_: owner.kernel_boot_identity
    )
    monkeypatch.setattr(worker, "native_process_entries", lambda *_: ())
    monkeypatch.setattr(
        worker,
        "capture_native_data",
        lambda *_a, **_k: native.model_copy(update={"observed_at": now}),
    )
    local = control.local_inventory(owner.intent.original_plan)
    peer = control.peer_inventory(owner.intent.original_plan)
    control.local_inventory = Mock(return_value=local.model_copy(update={"observed_at": now}))
    control.peer_inventory = Mock(return_value=peer.model_copy(update={"observed_at": now}))
    control._read = Mock(
        side_effect=lambda _path, model, *_a, **kw: (
            context if model is UpgradeColdContinuationContext else kw["payload"]
        )
    )
    body = UpgradeColdContinuationObserverInput(
        previous_recovery_id=owner.intent.recovery_id,
        read=UpgradeColdContinuationRead(
            previous_intent_sha256=owner.intent.sha256(), previous_owner_sha256=owner.sha256()
        ),
        observer_release=owner.intent.support_release,
        observer_runtime=runtime,
    )
    return worker, settings, body, control, bootstrap, run, current, source


def archived_observer_fixture(tmp_path, monkeypatch, *, failed_pair=False):
    from coire_node import expired_recovery_cold_archive as archiver

    worker, settings, body, control, bootstrap, run, current, source = observer_fixture(
        tmp_path, monkeypatch
    )
    now = source.observed_at + timedelta(seconds=1)
    run.write("cold.source.launch.0.json", source.launch)
    monkeypatch.setattr(archiver, "datetime", SimpleNamespace(now=lambda _: now))
    snapshots = {}
    for name in ("local_inventory", "peer_inventory"):
        snapshot = getattr(control, name)()
        snapshots[name] = snapshot
        setattr(control, name, Mock(return_value=snapshot.model_copy(update={"observed_at": now})))
    owner = source.launch.source.projection.owner
    with (
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, owner.sha256(), descriptor),
    ):
        archive = archiver.archive_cold_collectives(settings, source, control)
    for name, snapshot in snapshots.items():
        setattr(
            control,
            name,
            Mock(
                return_value=snapshot.model_copy(
                    update={
                        "owners": tuple(
                            o.model_copy(update={"entries": ()}) for o in snapshot.owners
                        )
                    }
                )
            ),
        )
    if failed_pair:
        from upgrade_cold_continuation_fixtures import failed_pair_after_archive

        smoke, result = failed_pair_after_archive(source, archive, now)
        run.write("cold.smoke.command.json", smoke.smoke)
        run.write("cold.smoke.receipt.json", smoke)
        run.write("cold.pair.0.command.json", result.dispatch)
        run.write("cold.pair.0.result.json", result)
        context = control._read("fixture", UpgradeColdContinuationContext).model_copy(
            update={"previous_smoke_command": smoke.smoke}
        )
        control._read.side_effect = lambda _path, model, *_a, **kw: (
            context if model is UpgradeColdContinuationContext else kw["payload"]
        )
    return worker, settings, body, control, bootstrap, run, current, source


def test_failed_pair_continuation_binds_actual_prior_records_without_copying_qualification(
    tmp_path, monkeypatch
):
    worker, settings, body, control, _, run, _, _ = archived_observer_fixture(
        tmp_path, monkeypatch, failed_pair=True
    )
    files = ("cold.smoke.receipt.json", "cold.pair.0.command.json", "cold.pair.0.result.json")
    before = {name: run.path(name).read_bytes() for name in files}
    proof = worker.observe_continuation(settings, body, control)
    assert proof.previous_smoke is not None
    assert proof.binding.previous_smoke_sha256 == proof.previous_smoke.sha256()
    assert len(proof.previous_pair_results) == 1
    assert not proof.previous_pair_results[0].qualified()
    assert proof.binding.previous_pair_result_sha256s == (proof.previous_pair_results[0].sha256(),)
    assert {name: run.path(name).read_bytes() for name in files} == before


@pytest.mark.parametrize("fault", [None, "archive_changed", "two_namespaces", "partial_pair"])
def test_archived_continuation_requires_exact_completed_history_and_current_idle_owners(
    tmp_path, monkeypatch, fault
):
    worker, settings, body, control, _bootstrap, run, _, source = archived_observer_fixture(
        tmp_path, monkeypatch
    )
    owner = source.launch.source.projection.owner
    history = run.root / "collective-history" / owner.original_claim.job_id
    original = {p.relative_to(history): p.read_bytes() for p in history.rglob("*") if p.is_file()}
    if fault == "archive_changed":
        scope = history / "candidate/0/scope.json"
        scope.write_bytes(scope.read_bytes() + b" ")
    elif fault == "two_namespaces":
        PrivateUpgradeStore(
            Path(settings.node_state_dir) / "upgrades/collectives" / owner.original_claim.job_id
        )
    elif fault == "partial_pair":
        # No completed typed result may be inferred from an unacknowledged dispatch.
        run.path("cold.pair.0.command.json").write_text("{}")
        run.path("cold.pair.0.command.json").chmod(0o600)
    if fault:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable, ValidationError)):
            worker.observe_continuation(settings, body, control)
        return
    proof = worker.observe_continuation(settings, body, control)
    assert proof.previous_archive is not None
    assert proof.binding.previous_archive_sha256 == proof.previous_archive.sha256()
    assert proof.binding.previous_history_sha256 == proof.previous_archive.archive.history_sha256
    assert proof.previous_pair_results == () and proof.previous_smoke is None
    assert proof.interrupted_scope_sha256s == owner.intent.request.original_scope_sha256s
    assert {
        p.relative_to(history): p.read_bytes() for p in history.rglob("*") if p.is_file()
    } == original


@pytest.mark.parametrize(
    "fault",
    [None, "source_gone", "owner_alive", "late", "unknown", "data", "exclusion", "foreign_pointer"],
)
def test_continuation_observer_refuses_uncertainty_and_keeps_effect_preimages(
    tmp_path, monkeypatch, fault
):
    worker, settings, body, control, bootstrap, run, current, source = observer_fixture(
        tmp_path, monkeypatch
    )
    original = {
        name: run.path(name).read_bytes()
        for name in (
            "cold.claim.json",
            "cold.activation.json",
            "cold.source.intent.json",
            "cold.source.receipt.json",
            "cold.source.process.0.json",
        )
    }
    if fault == "released":
        store = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades")
        from coire_core.models.upgrades import UpgradeMaintenance

        fence = store.read("maintenance.json", UpgradeMaintenance)
        store.write("maintenance.json", fence.model_copy(update={"state": "released"}))
    elif fault == "source_gone":
        monkeypatch.setattr(worker, "_owned_node_process", lambda *_: None)
    elif fault == "owner_alive":

        def retired(identity):
            if identity == source.launch.source.projection.owner.process:
                raise UpgradeUnavailable("fixture owner remains present")

        monkeypatch.setattr(worker, "verify_retired_identity", retired)
    elif fault == "late":
        run.path("cold.collective.archive.intent.json").write_text("{}")
    elif fault == "unknown":
        snapshot = control.local_inventory.return_value
        control.local_inventory.return_value = snapshot.model_copy(
            update={
                "owners": tuple(
                    o.model_copy(update={"status": "unknown"}) if o.owner == "runs" else o
                    for o in snapshot.owners
                )
            }
        )
    elif fault == "data":
        old = worker.capture_native_data(settings, body)
        monkeypatch.setattr(
            worker,
            "capture_native_data",
            lambda *_a, **_k: old.model_copy(update={"total_bytes": 1}),
        )
    elif fault == "foreign_pointer":
        foreign = (
            tmp_path
            / "foreign"
            / source.launch.source.projection.source_selection.runtime.environment_id
        )
        foreign.mkdir(parents=True)
        current.unlink()
        current.symlink_to(foreign)
    if fault == "exclusion":
        records = PrivateUpgradeStore(bootstrap.root.parent / "fixture-records")
        with records.ownership("fixture-supervisor.lock"), pytest.raises(UpgradeConflict):
            worker.observe_continuation(settings, body, control)
    elif fault:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable, ValidationError)):
            worker.observe_continuation(settings, body, control)
    else:
        proof = worker.observe_continuation(settings, body, control)
        assert proof.binding.previous_source_sha256 == source.sha256()
        assert proof.previous_source.process == source.process
        assert run.path("cold.continuation.observation." + proof.sha256() + ".json").is_file()
    assert all(run.path(name).read_bytes() == payload for name, payload in original.items())
    assert (
        bootstrap.read("status.json", NativeGuardStatus).process
        == source.launch.source.projection.owner.process
    )


@pytest.mark.parametrize("state", ["fencing", "draining", "active"])
def test_continuation_observer_accepts_legacy_retained_fence_states_with_canonical_idle_proof(
    tmp_path, monkeypatch, state
):
    from coire_core.models.upgrades import UpgradeMaintenance

    worker, settings, body, control, _bootstrap, _run, _current, _source = observer_fixture(
        tmp_path, monkeypatch
    )
    store = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades")
    fence = store.read("maintenance.json", UpgradeMaintenance)
    store.write("maintenance.json", fence.model_copy(update={"state": state}))
    proof = worker.observe_continuation(settings, body, control)
    assert proof.binding.original_maintenance_retained is True
    assert store.read("maintenance.json", UpgradeMaintenance).state == state


@pytest.mark.parametrize(
    "fault", [None, "pending_request", "changed_results", "foreign_request", "committed"]
)
def test_completed_qualification_is_preserved_only_as_history(tmp_path, monkeypatch, fault):
    from uuid import NAMESPACE_URL, uuid4, uuid5

    from coire_core.models.sharding import ProbeOutcome
    from coire_core.models.upgrade_recovery import (
        UpgradeExpiredRecoveryColdPairQualification,
        UpgradeExpiredRecoveryColdPairResult,
        UpgradeExpiredRecoveryColdSmokeReceipt,
        UpgradeExpiredRecoveryColdVerificationRequest,
    )

    worker, settings, body, control, _, run, _, source = archived_observer_fixture(
        tmp_path, monkeypatch, failed_pair=True
    )
    old = run.read("cold.pair.0.result.json", UpgradeExpiredRecoveryColdPairResult)
    smoke = run.read("cold.smoke.receipt.json", UpgradeExpiredRecoveryColdSmokeReceipt)
    owner = source.launch.source.projection.owner
    at = smoke.observed_at
    results = []
    for index in range(3):
        dispatch = old.dispatch.model_copy(
            update={
                "index": index,
                "command": old.dispatch.command.model_copy(
                    update={
                        "command_id": uuid5(
                            NAMESPACE_URL,
                            f"coire-expired-recovery-pair/{owner.intent.recovery_id}/{owner.intent.sha256()}/{index}",
                        )
                    }
                ),
            }
        )
        result = UpgradeExpiredRecoveryColdPairResult(
            dispatch=dispatch,
            observation=old.observation.model_copy(
                update={
                    "id": uuid4(),
                    "outcome": ProbeOutcome.SUCCEEDED,
                    "reason": None,
                    "runtime_sha256_a": owner.original_native.source.runtime.environment_sha256,
                    "runtime_sha256_b": owner.intent.original_plan.peer_runtime_sha256,
                    "bandwidth_bytes_per_second": 1024,
                    "latency_ms": 1.0,
                }
            ),
        )
        run.write(f"cold.pair.{index}.command.json", dispatch)
        run.write(f"cold.pair.{index}.result.json", result)
        results.append(result)
    qualification = UpgradeExpiredRecoveryColdPairQualification(
        results=tuple(results),
        local=control.local_inventory().model_copy(update={"observed_at": at}),
        peer=control.peer_inventory().model_copy(update={"observed_at": at}),
        observed_at=at,
    )
    run.write("cold.pair.qualification.json", qualification)
    context = control._read("fixture", UpgradeColdContinuationContext)
    request = UpgradeExpiredRecoveryColdVerificationRequest(
        smoke=smoke,
        archive=old.dispatch.archive,
        pair=qualification,
        native_after=context.native_baseline.model_copy(update={"observed_at": at}),
    )
    if fault in {"pending_request", "foreign_request"}:
        if fault == "foreign_request":
            request = request.model_copy(update={"archive": None})
        run.write("cold.verification.request.json", request)
    elif fault == "changed_results":
        run.write(
            "cold.pair.1.result.json",
            results[1].model_copy(
                update={"observation": results[1].observation.model_copy(update={"id": uuid4()})}
            ),
        )
    elif fault == "committed":
        run.path("cold.verification.json").write_text("{}")
    before = {p.name: p.read_bytes() for p in run.root.glob("*.json")}
    if fault in {"changed_results", "foreign_request", "committed"}:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable, ValidationError)):
            worker.observe_continuation(settings, body, control)
    else:
        proof = worker.observe_continuation(settings, body, control)
        assert proof.binding.previous_pair_qualification_sha256 == qualification.sha256()
        assert proof.binding.previous_verification_request_sha256 == (
            request.sha256() if fault == "pending_request" else None
        )
        assert len(proof.previous_pair_results) == 3
        assert all(result.qualified() for result in proof.previous_pair_results)
        # The proof has no executable new smoke or pair command.
        assert proof.previous_owner == owner
    assert {name: run.path(name).read_bytes() for name in before} == before
    added = {p.name for p in run.root.glob("*.json")} - before.keys()
    assert added == (
        {f"cold.continuation.observation.{proof.sha256()}.json"}
        if fault in {None, "pending_request"}
        else set()
    )
