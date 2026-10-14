"""Actual threaded capability and publisher retirement before Source handback."""

import threading
from types import SimpleNamespace

import pytest
from test_upgrade_recovery_cold_source import source_fixture

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import NativeGuardStatus
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_lease import bind_recovery_lease


@pytest.mark.parametrize("fault", [None, "revoked", "stop_during_gate"])
def test_readiness_thread_reauthorizes_held_scope_and_retires_before_handback(
    tmp_path, monkeypatch, fault
):
    from coire_node import expired_recovery_cold_heartbeat as worker

    selector, settings, owner, bootstrap, _, _, _, control = source_fixture(tmp_path, monkeypatch)
    entered, published = threading.Event(), threading.Event()
    monkeypatch.setattr(
        worker.psutil,
        "Process",
        lambda _: SimpleNamespace(create_time=lambda: owner.process.create_time_s),
    )

    def gate(claim):
        entered.set()
        if fault == "revoked":
            raise UpgradeUnavailable("fixture revoked")
        if fault == "stop_during_gate":
            threading.Event().wait(0.2)
        return claim

    monkeypatch.setattr(
        worker, "NativeGuardTransport", lambda *_: SimpleNamespace(verify_cold_recovery_owner=gate)
    )
    original_write = PrivateUpgradeStore.write

    def write(store, key, value, **kw):
        result = original_write(store, key, value, **kw)
        if key == "status.json":
            published.set()
        return result

    with (
        bootstrap.ownership("lifetime.lock"),
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, owner.sha256(), descriptor),
    ):
        selection = selector.project_cold_source(settings, owner, control)
        before = bootstrap.read("status.json", NativeGuardStatus)
        monkeypatch.setattr(PrivateUpgradeStore, "write", write)
        if fault == "revoked":
            with (
                pytest.raises(UpgradeUnavailable, match="publication failed"),
                worker.cold_readiness_heartbeat(settings, selection),
            ):
                assert entered.wait(1)
        else:
            with worker.cold_readiness_heartbeat(settings, selection):
                assert entered.wait(1)
                if fault is None:
                    assert published.wait(1)
        after = bootstrap.read("status.json", NativeGuardStatus)
        if fault is None:
            assert after.ready and after.process == owner.process
            assert after.selected == selection.projection.source_selection
        else:
            assert after == before
        # No publisher survives the context to race Source's later status write.
        assert not any(thread.name == "coire-cold-readiness" for thread in threading.enumerate())
