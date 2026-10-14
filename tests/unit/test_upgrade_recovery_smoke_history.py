"""Preserve exact completed legacy smoke bytes through interrupted namespace moves."""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_cold_source import source_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import NativeSmokeProgress, UpgradeEvidence, UpgradeOwnerInventory
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_lease import bind_recovery_lease


@pytest.mark.parametrize("fault", [None, "rename", "intent", "receipt", "busy", "changed"])
def test_completed_smoke_archive_preserves_history_and_reconciles_lost_writes(
    tmp_path, monkeypatch, fault
):
    from coire_node import expired_recovery_smoke_history as worker

    selector, settings, claim, bootstrap, run, _, _, control = source_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "native_process_entries", Mock(return_value=()))
    scopes = PrivateUpgradeStore(tmp_path / "state/upgrades/smokes")
    before = {}
    for index in range(65):
        job = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
        name = f"{job}-{index + 1}-{'a' * 64}-post"
        scope = PrivateUpgradeStore(scopes.path(name))
        scope.write("owner.json", UpgradeOwnerInventory(owner="engines", status="complete"))
        scope.write(
            "progress.json",
            NativeSmokeProgress(
                job_id=job,
                generation=index + 1,
                runtime_sha256="a" * 64,
                phase="complete",
                observed_at=datetime.now(UTC),
            ),
        )
        scope.write("result.json", UpgradeEvidence(runtime_sha256="a" * 64, cleanup_verified=True))
        engine = PrivateUpgradeStore(scope.path("engine"))
        engine.path("engines.json").write_text("[]")
        engine.path("engines.json").chmod(0o600)
        with scope.ownership("smoke.lock"):
            pass
        before[name] = {
            p.relative_to(scope.root): p.read_bytes() for p in scope.root.rglob("*") if p.is_file()
        }
    if fault == "busy":
        worker.native_process_entries.return_value = (object(),)
    write, rename = PrivateUpgradeStore.write, worker.os.rename
    interrupted = False

    def persist(store, key, value, **kwargs):
        nonlocal interrupted
        result = write(store, key, value, **kwargs)
        if (
            fault in {"intent", "receipt"}
            and key == f"cold.smoke.history.{fault}.json"
            and not interrupted
        ):
            interrupted = True
            raise UpgradeUnavailable("lost write")
        return result

    def move(old, new):
        nonlocal interrupted
        result = rename(old, new)
        if fault == "rename" and not interrupted:
            interrupted = True
            raise UpgradeUnavailable("lost rename")
        return result

    monkeypatch.setattr(PrivateUpgradeStore, "write", persist)
    monkeypatch.setattr(worker.os, "rename", move)
    with (
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, claim.sha256(), descriptor),
    ):
        selection = selector.project_cold_source(settings, claim, control)
        if fault == "busy":
            with pytest.raises(UpgradeUnavailable, match="actual engine"):
                worker.archive_completed_smokes(settings, selection, control)
            assert len(list(scopes.root.iterdir())) == 65
            return
        if fault in {"rename", "intent", "receipt"}:
            with pytest.raises(UpgradeUnavailable, match="lost"):
                worker.archive_completed_smokes(settings, selection, control)
        proof = worker.archive_completed_smokes(settings, selection, control)
        assert proof is not None and len(proof.entries) == 5
        assert worker.archive_completed_smokes(settings, selection, control) == proof
        assert len(list(scopes.root.iterdir())) == 60
        for entry in proof.entries:
            archived = run.root / "completed-smoke-history" / entry.namespace
            assert {
                p.relative_to(archived): p.read_bytes() for p in archived.rglob("*") if p.is_file()
            } == before[entry.namespace]
        if fault == "changed":
            (
                run.root
                / "completed-smoke-history"
                / proof.entries[0].namespace
                / "engine/engines.json"
            ).write_text("[1]")
            with pytest.raises(UpgradeConflict, match="bytes differ"):
                worker.archive_completed_smokes(settings, selection, control)
