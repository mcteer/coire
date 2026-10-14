"""Actual retained namespace archive, never a manufactured collective result."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from test_upgrade_recovery_cold_source import source_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.sharding import LinkObservation, ProbeOutcome, ProbeTransport
from coire_core.models.upgrade_collective import NodeUpgradeCollectiveResult
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
    UpgradeExpiredRecoveryColdSourceLaunch,
    UpgradeExpiredRecoveryColdSourceProcess,
)
from coire_core.models.upgrades import (
    NODE_UPGRADE_OWNERS,
    NodeAgentProcessIdentity,
    NodeUpgradeCollectiveRead,
    NodeUpgradeCollectiveScope,
    NodeUpgradeInventory,
    UpgradeInventoryEntry,
    UpgradeOwnerInventory,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_lease import bind_recovery_lease


def archive_fixture(tmp_path, monkeypatch, *, supervision="launchd"):
    from coire_node import expired_recovery_cold_archive as worker

    selector, settings, claim, bootstrap, run, _guard, current, control = source_fixture(
        tmp_path, monkeypatch, supervision=supervision
    )
    plan = claim.intent.original_plan
    native = claim.original_native
    scope = NodeUpgradeCollectiveScope(
        plan=plan,
        request=NodeUpgradeCollectiveRead(
            generation=native.generation,
            plan_sha256=native.plan_sha256,
            stage="candidate",
            runtime_sha256=native.candidate.environment_sha256,
            deadline_at=native.recovery_started_at + timedelta(seconds=120),
        ),
        index=0,
        command_id=uuid5(
            NAMESPACE_URL,
            f"coire-upgrade-collective/{native.job_id}/candidate/{native.plan_sha256}/0",
        ),
    )
    parent = PrivateUpgradeStore(tmp_path / "state/upgrades/collectives")
    history = PrivateUpgradeStore(parent.path(native.job_id))
    stage = PrivateUpgradeStore(history.path("candidate"))
    slot = PrivateUpgradeStore(stage.path("0"))
    slot.write("scope.json", scope)
    with slot.ownership("collective.lock"):
        pass
    logical = UpgradeInventoryEntry(
        owner="probes",
        kind="upgrade-collective",
        work_id=str(scope.command_id),
        state="cleaning",
        holds=("unresolved-collective",),
    )

    def inventory(peer=False):
        return NodeUpgradeInventory(
            job_id=native.job_id,
            generation=native.generation,
            node="coire-edge-b" if peer else "coire-edge-a",
            runtime_sha256=plan.peer_runtime_sha256
            if peer
            else native.source.runtime.environment_sha256,
            observed_at=datetime.now(UTC),
            owners=tuple(
                UpgradeOwnerInventory(
                    owner=name,
                    status="complete",
                    entries=(logical,) if name == "probes" and not peer else (),
                )
                for name in sorted(NODE_UPGRADE_OWNERS)
            ),
        )

    control.local_inventory = Mock(side_effect=lambda *_args, **_kw: inventory())
    control.peer_inventory = Mock(side_effect=lambda *_args, **_kw: inventory(True))
    monkeypatch.setattr(worker, "require_live_cold_source", lambda *_: None)
    return worker, selector, settings, claim, bootstrap, run, current, history, slot, control


@pytest.mark.parametrize("interruption", [None, "intent", "effect", "rename", "receipt"])
def test_cold_archive_preserves_exact_original_bytes_at_every_write_boundary(
    tmp_path, monkeypatch, interruption
):
    worker, selector, settings, claim, bootstrap, run, _, history, _slot, control = archive_fixture(
        tmp_path, monkeypatch
    )
    original = {
        p.relative_to(history.root): p.read_bytes() for p in history.root.rglob("*") if p.is_file()
    }
    write = PrivateUpgradeStore.write
    rename = worker.os.rename
    interrupted = False

    def persist(store, key, value, **kw):
        nonlocal interrupted
        result = write(store, key, value, **kw)
        if not interrupted and key == f"cold.collective.archive.{interruption}.json":
            interrupted = True
            raise UpgradeUnavailable("fixture lost durable write response")
        return result

    def move(old, new):
        nonlocal interrupted
        result = rename(old, new)
        if interruption == "rename" and not interrupted:
            interrupted = True
            raise UpgradeUnavailable("fixture lost rename response")
        return result

    monkeypatch.setattr(PrivateUpgradeStore, "write", persist)
    monkeypatch.setattr(worker.os, "rename", move)
    with (
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, claim.sha256(), descriptor),
    ):
        selected = selector.project_cold_source(settings, claim, control)
        launch = UpgradeExpiredRecoveryColdSourceLaunch(
            source=selected, attempt=0, started_at=datetime.now(UTC)
        )
        source = UpgradeExpiredRecoveryColdSourceProcess(
            launch=launch,
            process=NodeAgentProcessIdentity(
                pid=98765,
                create_time_s=datetime.now(UTC).timestamp(),
                environment_sha256=claim.original_native.source.runtime.environment_sha256,
            ),
            observed_at=datetime.now(UTC),
        )
        if interruption:
            with pytest.raises(UpgradeUnavailable, match="lost"):
                worker.archive_cold_collectives(settings, source, control)
        receipt = worker.archive_cold_collectives(settings, source, control)
        assert worker.archive_cold_collectives(settings, source, control) == receipt
    archived = run.root / "collective-history" / claim.original_claim.job_id
    assert not history.root.exists()
    assert {
        p.relative_to(archived): p.read_bytes() for p in archived.rglob("*") if p.is_file()
    } == original
    assert not (archived / "candidate/0/result.json").exists()
    assert receipt.archive.interrupted_scope_sha256s == claim.intent.request.original_scope_sha256s
    assert receipt.archive.scopes[0].request.deadline_at < claim.claimed_at
    assert (
        run.read(
            "cold.collective.archive.receipt.json",
            UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
        )
        == receipt
    )


@pytest.mark.parametrize("interrupted_slot", [0, 1, 2])
def test_archive_retains_three_original_scopes_and_only_the_unresolved_obligation(
    tmp_path, monkeypatch, interrupted_slot
):
    worker, selector, settings, claim, bootstrap, run, _, history, first, control = archive_fixture(
        tmp_path, monkeypatch
    )
    original_scope = first.read("scope.json", NodeUpgradeCollectiveScope)
    scopes = []
    for index in range(3):
        scope = original_scope.model_copy(
            update={
                "index": index,
                "command_id": uuid5(
                    NAMESPACE_URL,
                    f"coire-upgrade-collective/{claim.original_claim.job_id}/candidate/"
                    f"{claim.original_claim.plan_sha256}/{index}",
                ),
            }
        )
        slot = PrivateUpgradeStore(history.root / "candidate" / str(index))
        slot.write("scope.json", scope)
        with slot.ownership("collective.lock"):
            pass
        if index != interrupted_slot:
            result = NodeUpgradeCollectiveResult(
                job_id=scope.plan.operation.job_id,
                generation=scope.request.generation,
                plan_sha256=scope.request.plan_sha256,
                stage=scope.request.stage,
                deadline_at=scope.request.deadline_at,
                observations=(
                    LinkObservation(
                        id=uuid4(),
                        node_a="coire-edge-a",
                        node_b="coire-edge-b",
                        transport=ProbeTransport.JACCL,
                        outcome=ProbeOutcome.FAILED,
                        os_version_a="synthetic",
                        os_version_b="synthetic",
                        engine_version="synthetic",
                        observed_at=scope.request.deadline_at - timedelta(seconds=1),
                    ),
                ),
            )
            slot.write("result.json", result)
        scopes.append(scope)
    request = claim.intent.request.model_copy(
        update={"original_scope_sha256s": tuple(scope.sha256() for scope in scopes)}
    )
    claim = claim.model_copy(
        update={"intent": claim.intent.model_copy(update={"request": request})}
    )
    run.write("cold.claim.json", claim)
    run.write("cold.activation.json", claim)
    inventory = control.local_inventory()
    owners = tuple(
        owner.model_copy(
            update={
                "entries": tuple(
                    entry.model_copy(update={"work_id": str(scopes[interrupted_slot].command_id)})
                    for entry in owner.entries
                )
            }
        )
        for owner in inventory.owners
    )
    control.local_inventory = Mock(
        side_effect=lambda *_a, **_kw: inventory.model_copy(
            update={"owners": owners, "observed_at": datetime.now(UTC)}
        )
    )
    original = {
        p.relative_to(history.root): p.read_bytes() for p in history.root.rglob("*") if p.is_file()
    }
    with (
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, claim.sha256(), descriptor),
    ):
        selected = selector.project_cold_source(settings, claim, control)
        source = UpgradeExpiredRecoveryColdSourceProcess(
            launch=UpgradeExpiredRecoveryColdSourceLaunch(
                source=selected, attempt=0, started_at=datetime.now(UTC)
            ),
            process=NodeAgentProcessIdentity(
                pid=98765,
                create_time_s=datetime.now(UTC).timestamp(),
                environment_sha256=claim.original_native.source.runtime.environment_sha256,
            ),
            observed_at=datetime.now(UTC),
        )
        receipt = worker.archive_cold_collectives(settings, source, control)
    archived = run.root / "collective-history" / claim.original_claim.job_id
    assert {
        p.relative_to(archived): p.read_bytes() for p in archived.rglob("*") if p.is_file()
    } == original
    assert receipt.archive.scopes == tuple(scopes)
    assert receipt.archive.interrupted_scope_sha256s == (scopes[interrupted_slot].sha256(),)
    assert not (archived / "candidate" / str(interrupted_slot) / "result.json").exists()


@pytest.mark.parametrize("interruption", [None, "intent", "effect", "rename", "receipt"])
def test_continuation_moves_preserved_archive_once_without_recreating_logical_obligations(
    tmp_path, monkeypatch, interruption
):
    from test_upgrade_recovery_cold_retained_source import continuation_fixture

    from coire_node import expired_recovery_cold_archive as worker

    selector, _, _, settings, owner, bootstrap, run, _, retained, control = continuation_fixture(
        tmp_path, monkeypatch, archived=True
    )
    now = owner.claimed_at + timedelta(seconds=1)
    monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda _: now))
    old_history = (
        run.root.parent
        / owner.continuation.previous_recovery_id
        / "collective-history"
        / owner.original_claim.job_id
    )
    original = {
        p.relative_to(old_history): p.read_bytes() for p in old_history.rglob("*") if p.is_file()
    }
    for name in ("local_inventory", "peer_inventory"):
        snapshot = getattr(control, name)()
        setattr(control, name, Mock(return_value=snapshot.model_copy(update={"observed_at": now})))
    write, rename = PrivateUpgradeStore.write, worker.os.rename
    lost = False

    def persist(store, key, value, **kw):
        nonlocal lost
        result = write(store, key, value, **kw)
        if not lost and key == f"cold.collective.archive.{interruption}.json":
            lost = True
            raise UpgradeUnavailable("lost continuation archive response")
        return result

    def move(old, new):
        nonlocal lost
        result = rename(old, new)
        if interruption == "rename" and not lost:
            lost = True
            raise UpgradeUnavailable("lost continuation archive rename")
        return result

    with (
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, owner.sha256(), descriptor),
    ):
        selected = selector.project_cold_source(settings, owner, control)
        source = UpgradeExpiredRecoveryColdSourceProcess(
            launch=UpgradeExpiredRecoveryColdSourceLaunch(
                source=selected, attempt=0, started_at=now, retained_process=retained.process
            ),
            process=retained.process,
            observed_at=now,
        )
        monkeypatch.setattr(PrivateUpgradeStore, "write", persist)
        monkeypatch.setattr(worker.os, "rename", move)
        if interruption:
            with pytest.raises(UpgradeUnavailable, match="lost continuation"):
                worker.archive_cold_collectives(settings, source, control)
        receipt = worker.archive_cold_collectives(settings, source, control)
        assert worker.archive_cold_collectives(settings, source, control) == receipt
    history = run.root / "collective-history" / owner.original_claim.job_id
    assert not old_history.exists()
    assert {
        p.relative_to(history): p.read_bytes() for p in history.rglob("*") if p.is_file()
    } == original
    assert receipt.archive.previous_archive_sha256 == owner.continuation.previous_archive_sha256
    assert receipt.archive.interrupted_scope_sha256s == owner.intent.request.original_scope_sha256s


@pytest.mark.parametrize(
    "fault", ["busy_peer", "unknown_local", "foreign_file", "missing_owner", "revoked"]
)
def test_cold_archive_refuses_uncertainty_and_preserves_original_namespace(
    tmp_path, monkeypatch, fault
):
    worker, selector, settings, claim, bootstrap, run, _, history, slot, control = archive_fixture(
        tmp_path, monkeypatch
    )
    if fault == "foreign_file":
        slot.path("foreign.json").write_text("{}")
        slot.path("foreign.json").chmod(0o600)
    with (
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, claim.sha256(), descriptor),
    ):
        selected = selector.project_cold_source(settings, claim, control)
        launch = UpgradeExpiredRecoveryColdSourceLaunch(
            source=selected, attempt=0, started_at=datetime.now(UTC)
        )
        source = UpgradeExpiredRecoveryColdSourceProcess(
            launch=launch,
            process=NodeAgentProcessIdentity(
                pid=98765,
                create_time_s=datetime.now(UTC).timestamp(),
                environment_sha256=claim.original_native.source.runtime.environment_sha256,
            ),
            observed_at=datetime.now(UTC),
        )
        if fault in {"busy_peer", "unknown_local", "missing_owner"}:
            original = control.peer_inventory if fault == "busy_peer" else control.local_inventory
            snapshot = original()
            owners = list(snapshot.owners)
            if fault == "missing_owner":
                owners.pop()
            else:
                owners[0] = owners[0].model_copy(update={"status": "unknown"})
            original.side_effect = None
            original.return_value = snapshot.model_copy(update={"owners": tuple(owners)})
        if fault == "revoked":
            control.verify_cold_recovery_owner.side_effect = UpgradeUnavailable("fixture revoked")
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
            worker.archive_cold_collectives(settings, source, control)
    assert history.root.exists()
    assert (
        run.read(
            "cold.collective.archive.receipt.json",
            UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
        )
        is None
    )


@pytest.mark.parametrize("interruption", [None, "intent", "effect", "receipt"])
def test_absent_original_dispatch_is_positive_replayable_evidence(
    tmp_path, monkeypatch, interruption
):
    import shutil

    worker, selector, settings, claim, bootstrap, run, _, history, _, control = archive_fixture(
        tmp_path, monkeypatch
    )
    shutil.rmtree(history.root)
    request = claim.intent.request.model_copy(update={"original_scope_sha256s": ()})
    intent = claim.intent.model_copy(update={"request": request, "scopes": ()})
    claim = claim.model_copy(update={"intent": intent})
    run.write("cold.claim.json", claim)
    run.write("cold.activation.json", claim)
    local = control.local_inventory()
    control.local_inventory.side_effect = None
    control.local_inventory.return_value = local.model_copy(
        update={"owners": tuple(owner.model_copy(update={"entries": ()}) for owner in local.owners)}
    )
    write = PrivateUpgradeStore.write
    interrupted = False

    def persist(store, key, value, **kwargs):
        nonlocal interrupted
        result = write(store, key, value, **kwargs)
        if (
            interruption
            and not interrupted
            and key == f"cold.collective.archive.{interruption}.json"
        ):
            interrupted = True
            raise UpgradeUnavailable("lost absence write response")
        return result

    monkeypatch.setattr(PrivateUpgradeStore, "write", persist)
    with (
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, claim.sha256(), descriptor),
    ):
        selected = selector.project_cold_source(settings, claim, control)
        source = UpgradeExpiredRecoveryColdSourceProcess(
            launch=UpgradeExpiredRecoveryColdSourceLaunch(
                source=selected, attempt=0, started_at=datetime.now(UTC)
            ),
            process=NodeAgentProcessIdentity(
                pid=98765,
                create_time_s=datetime.now(UTC).timestamp(),
                environment_sha256=claim.original_native.source.runtime.environment_sha256,
            ),
            observed_at=datetime.now(UTC),
        )
        if interruption:
            with pytest.raises(UpgradeUnavailable, match="lost absence"):
                worker.archive_cold_collectives(settings, source, control)
        receipt = worker.archive_cold_collectives(settings, source, control)
        assert receipt == worker.archive_cold_collectives(settings, source, control)
        assert receipt.archive.scopes == () and receipt.archive.interrupted_scope_sha256s == ()
        assert not history.root.exists()
        assert not run.path("collective-history").joinpath(claim.original_claim.job_id).exists()
        history.root.mkdir(mode=0o700)
        with pytest.raises(UpgradeConflict, match="has a namespace"):
            worker.archive_cold_collectives(settings, source, control)
