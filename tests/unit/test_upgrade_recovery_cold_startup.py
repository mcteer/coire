"""Readiness retry is bounded by retained launch epoch, never a second spawn."""

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_cold_archive import archive_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdSourceLaunch,
    UpgradeExpiredRecoveryColdSourceProcess,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity, NodeUpgradeStatus
from coire_node.recovery_lease import bind_recovery_lease


@pytest.mark.parametrize("fault", [None, "foreign", "expired", "revoked", "stopped"])
def test_startup_wait_preserves_original_launch_window_and_refuses_unknown_source(
    tmp_path, monkeypatch, fault
):
    from coire_node import expired_recovery_cold_controller as worker
    from coire_node import expired_recovery_cold_launch as launcher

    _, selector, settings, owner, bootstrap, _, _, _, _, control = archive_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(launcher, "require_live_cold_source", lambda *_: None)
    stopped = threading.Event()
    with (
        bootstrap.ownership("lifetime.lock"),
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, owner.sha256(), descriptor),
    ):
        selection = selector.project_cold_source(settings, owner, control)
        now = datetime.now(UTC)
        started = now
        if fault == "expired":
            monkeypatch.setattr(
                worker, "datetime", SimpleNamespace(now=lambda _: now + timedelta(seconds=31))
            )
        source = UpgradeExpiredRecoveryColdSourceProcess(
            launch=UpgradeExpiredRecoveryColdSourceLaunch(
                source=selection, attempt=0, started_at=started
            ),
            process=NodeAgentProcessIdentity(
                pid=98765,
                create_time_s=started.timestamp(),
                environment_sha256=owner.original_native.source.runtime.environment_sha256,
            ),
            observed_at=now,
        )
        current = NodeUpgradeStatus(
            node=settings.node_name,
            bootstrap_ready=True,
            runtime_sha256="f" * 64
            if fault == "foreign"
            else owner.original_native.source.runtime.environment_sha256,
            release_sha256=owner.intent.request.expected_source_sha256,
            observed_at=now,
        )
        control.status = Mock(side_effect=[UpgradeUnavailable("starting"), current])
        if fault == "revoked":
            control.verify_cold_recovery_owner.side_effect = UpgradeUnavailable("revoked")
        if fault == "stopped":
            stopped.set()
            assert worker.wait_for_cold_source(settings, source, control, stopped) is False
            control.status.assert_not_called()
        elif fault == "foreign":
            with pytest.raises(UpgradeConflict, match="another physical runtime"):
                worker.wait_for_cold_source(settings, source, control, stopped)
        elif fault in {"expired", "revoked"}:
            with pytest.raises(UpgradeUnavailable):
                worker.wait_for_cold_source(settings, source, control, stopped)
            control.status.assert_not_called()
        else:
            assert worker.wait_for_cold_source(settings, source, control, stopped)
            assert control.status.call_count == 2
            assert all(
                call.kwargs["deadline_at"]
                == min(owner.intent.deadline_at, source.launch.started_at + timedelta(seconds=30))
                for call in control.status.call_args_list
            )


@pytest.mark.parametrize("fault", [None, "foreign_writer", "stale_writer"])
def test_released_claim_readiness_requires_actual_recovery_writer_lease(
    tmp_path, monkeypatch, fault
):
    from test_upgrade_recovery_cold_retained_source import continuation_fixture

    from coire_core.models.upgrades import NativeGuardStatus
    from coire_node import expired_recovery_cold_controller as worker

    selector, _, _, settings, owner, bootstrap, _, _, retained, control = continuation_fixture(
        tmp_path, monkeypatch, post_handback=True
    )
    now = owner.claimed_at + timedelta(seconds=1)
    monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(worker, "require_live_cold_source", lambda *_: None, raising=False)
    with (
        bootstrap.ownership("lifetime.lock") as lifetime,
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, owner.sha256(), descriptor, lifetime_descriptor=lifetime),
    ):
        selected = selector.project_cold_source(settings, owner, control)
        selector.publish_cold_source_readiness(settings, selected, control)
        source = UpgradeExpiredRecoveryColdSourceProcess(
            launch=UpgradeExpiredRecoveryColdSourceLaunch(
                source=selected, attempt=0, started_at=now, retained_process=retained.process
            ),
            process=retained.process,
            observed_at=now,
        )
        from coire_node.expired_recovery_cold_source import stores

        _, run, _ = stores(settings, owner)
        run.write("cold.source.launch.0.json", source.launch)
        run.write("cold.source.process.0.json", source)
        control.status = Mock(
            return_value=NodeUpgradeStatus(
                node=settings.node_name,
                bootstrap_ready=False,
                runtime_sha256=owner.original_native.source.runtime.environment_sha256,
                release_sha256=owner.intent.request.expected_source_sha256,
                observed_at=now,
            )
        )
        current = bootstrap.read("status.json", NativeGuardStatus)
        if fault == "foreign_writer":
            bootstrap.write(
                "status.json",
                current.model_copy(
                    update={
                        "process": current.process.model_copy(
                            update={"pid": current.process.pid + 1}
                        )
                    }
                ),
            )
        elif fault == "stale_writer":
            bootstrap.write(
                "status.json",
                current.model_copy(update={"observed_at": now - timedelta(seconds=6)}),
            )
        if fault is None:
            assert worker.wait_for_cold_source(settings, source, control, threading.Event())
        else:
            assert not worker.recovery_writer_ready(settings, source)
    with pytest.raises(UpgradeUnavailable):
        worker.recovery_writer_ready(settings, source)
