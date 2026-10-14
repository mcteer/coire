"""Interrupted dispatch closure is scoped, durable, lock-bound and replay-safe."""

from unittest.mock import Mock

import pytest
from test_collective_cleanup_inventory import inventories
from test_upgrade_collective import scope

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import NodeMaintenanceCommand, NodeUpgradeCollectiveCleanup
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.maintenance import NodeMaintenance


def setup(tmp_path):
    value = scope()
    gate = NodeMaintenance(tmp_path / "upgrades", "coire-edge-a")
    gate.apply(NodeMaintenanceCommand(maintenance=value.plan.maintenance))
    root = gate.store
    for part in ("collectives", value.plan.operation.job_id, value.request.stage, str(value.index)):
        root = PrivateUpgradeStore(root.path(part))
    root.write("scope.json", value, replace=False)
    settings = Settings(
        upgrades_enabled=True, node_name="coire-edge-a", node_state_dir=str(tmp_path)
    )
    return value, gate, root, settings


def test_cleanup_retains_proof_and_replay_has_no_second_observation(tmp_path):
    from coire_node.collective_cleanup import reconcile_collective_cleanup

    value, gate, root, settings = setup(tmp_path)
    fetch = Mock(return_value=inventories(value))
    request = NodeUpgradeCollectiveCleanup(scope=value)
    receipt = reconcile_collective_cleanup(settings, request, gate, fetch)
    assert receipt.scope_sha256 == value.sha256()
    assert gate.blocked()
    assert root.read("scope.json", type(value)) == value
    assert not root.path("result.json").exists()  # Not a synthetic measured failure.
    assert root.path("cleanup-local.json").exists()
    assert root.path("cleanup-peer.json").exists()
    assert reconcile_collective_cleanup(settings, request, gate, fetch) == receipt
    assert fetch.call_count == 1


def test_cleanup_outcomes_are_observable_without_scope_or_secret_metric_labels(
    tmp_path, monkeypatch, caplog
):
    from coire_node import collective_cleanup

    value, gate, root, settings = setup(tmp_path)
    request = NodeUpgradeCollectiveCleanup(scope=value)
    counter = Mock()
    monkeypatch.setattr(collective_cleanup, "cleanup_outcomes", counter)
    with caplog.at_level("INFO"):
        receipt = collective_cleanup.reconcile_collective_cleanup(
            settings, request, gate, Mock(return_value=inventories(value))
        )
        counter.add.assert_called_with(1, {"node": "coire-edge-a", "outcome": "interrupted"})
        root.path("cleanup-peer.json").unlink()
        with pytest.raises(UpgradeUnavailable):
            collective_cleanup.reconcile_collective_cleanup(settings, request, gate, Mock())
    counter.add.assert_called_with(1, {"node": "coire-edge-a", "outcome": "refused"})
    records = [record for record in caplog.records if record.name == collective_cleanup.__name__]
    assert [record.outcome for record in records] == ["interrupted", "refused"]
    assert all(
        record.job_id == receipt.job_id and record.command_id == str(value.command_id)
        for record in records
    )


def test_closed_interruption_never_relaunches_or_becomes_a_worker_result(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from coire_node import upgrade_collective
    from coire_node.collective_cleanup import reconcile_collective_cleanup

    value, gate, root, settings = setup(tmp_path)
    reconcile_collective_cleanup(
        settings,
        NodeUpgradeCollectiveCleanup(scope=value),
        gate,
        Mock(return_value=inventories(value)),
    )
    control = Mock()
    control.observed.return_value = object()
    control._guard_ready.return_value = True
    control.journal.return_value.current.return_value = SimpleNamespace(
        phase="candidate_selected",
        readiness_deadline_at=value.request.deadline_at,
    )
    monkeypatch.setattr(upgrade_collective, "NativeUpgradeControl", Mock(return_value=control))
    monkeypatch.setattr(
        upgrade_collective, "running_runtime_sha256", lambda _: value.runtime_pair()[0]
    )
    runner = Mock()
    with pytest.raises(UpgradeUnavailable, match="cleanup verified"):
        upgrade_collective.run_upgrade_collective(settings, value, gate, runner)
    runner.run.assert_not_called()
    assert not root.path("result.json").exists()
    assert gate.blocked()


def test_canonical_inventory_recognizes_only_proven_logical_closure(tmp_path):
    from coire_node.collective_cleanup import reconcile_collective_cleanup
    from coire_node.upgrade_inventory import collective_entries

    value, gate, root, settings = setup(tmp_path)
    assert len(collective_entries(tmp_path)) == 1
    reconcile_collective_cleanup(
        settings,
        NodeUpgradeCollectiveCleanup(scope=value),
        gate,
        Mock(return_value=inventories(value)),
    )
    assert collective_entries(tmp_path) == ()
    root.path("cleanup-peer.json").unlink()
    with pytest.raises(UpgradeUnavailable):
        collective_entries(tmp_path)


def test_partial_proof_write_is_not_closure_and_retry_observes_again(tmp_path, monkeypatch):
    from coire_node.collective_cleanup import reconcile_collective_cleanup
    from coire_node.upgrade_inventory import collective_entries

    value, gate, root, settings = setup(tmp_path)
    request = NodeUpgradeCollectiveCleanup(scope=value)
    fetch = Mock(side_effect=lambda: inventories(value))
    original_write = PrivateUpgradeStore.write

    def interrupted_write(self, key, document, **kwargs):
        if key == "cleanup.json":
            raise UpgradeUnavailable("simulated interrupted receipt persistence")
        return original_write(self, key, document, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(PrivateUpgradeStore, "write", interrupted_write)
        with pytest.raises(UpgradeUnavailable):
            reconcile_collective_cleanup(settings, request, gate, fetch)
    assert not root.path("cleanup.json").exists()
    assert len(collective_entries(tmp_path)) == 1
    reconcile_collective_cleanup(settings, request, gate, fetch)
    assert fetch.call_count == 2
    assert collective_entries(tmp_path) == ()
    assert root.read("scope.json", type(value)).request.deadline_at == value.request.deadline_at


def test_durable_closure_replays_historical_proof_without_fresh_qualification(
    tmp_path, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from coire_node import collective_cleanup
    from coire_node.upgrade_inventory import collective_entries

    value, gate, root, settings = setup(tmp_path)
    request = NodeUpgradeCollectiveCleanup(scope=value)
    receipt = collective_cleanup.reconcile_collective_cleanup(
        settings,
        request,
        gate,
        Mock(return_value=inventories(value)),
    )
    later = datetime.now(UTC) + timedelta(hours=1)

    class LaterClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(collective_cleanup, "datetime", LaterClock)
    fetch = Mock(side_effect=AssertionError("durable closure must not reobserve"))
    assert (
        collective_cleanup.reconcile_collective_cleanup(settings, request, gate, fetch) == receipt
    )
    assert collective_entries(tmp_path) == ()
    assert gate.blocked()
    assert not root.path("result.json").exists()


@pytest.mark.parametrize("fault", ["lock", "scope", "fence", "unknown", "tampered_proof"])
def test_cleanup_refuses_uncertain_or_foreign_authority(tmp_path, fault):
    from coire_node.collective_cleanup import reconcile_collective_cleanup

    value, gate, root, settings = setup(tmp_path)
    request = NodeUpgradeCollectiveCleanup(scope=value)
    fetch = Mock(return_value=inventories(value))
    if fault == "lock":
        with root.ownership("collective.lock"), pytest.raises(UpgradeConflict):
            reconcile_collective_cleanup(settings, request, gate, fetch)
        fetch.assert_not_called()
        return
    if fault == "scope":
        request = NodeUpgradeCollectiveCleanup(
            scope=value.model_copy(
                update={
                    "request": value.request.model_copy(
                        update={"deadline_at": value.request.deadline_at.replace(microsecond=0)}
                    )
                }
            )
        )
    elif fault == "fence":
        gate.store.write(
            "maintenance.json",
            value.plan.maintenance.model_copy(update={"generation": value.request.generation + 1}),
        )
    elif fault == "unknown":
        local, peer = inventories(value)
        peer = peer.model_copy(
            update={
                "owners": (
                    peer.owners[0].model_copy(update={"status": "unknown"}),
                    *peer.owners[1:],
                )
            }
        )
        fetch.return_value = local, peer
    else:
        reconcile_collective_cleanup(settings, request, gate, fetch)
        local, _ = inventories(value)
        root.write("cleanup-local.json", local.model_copy(update={"runtime_sha256": "f" * 64}))
    with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
        reconcile_collective_cleanup(settings, request, gate, fetch)
    if fault != "tampered_proof":
        assert not root.path("cleanup.json").exists()
