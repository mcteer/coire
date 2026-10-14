"""Durable supervisor boundaries with real fixture exclusion; native facts stubbed."""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_cold_handback import perform_source_handback_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdSupervisionReceipt
from coire_core.models.upgrades import NodeAgentProcessIdentity
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "intent", "receipt", "revoked", "launch", "startup"])
@pytest.mark.parametrize(
    "continuation",
    [False, True, "post_handback", "inherited"],
    ids=["cold", "continuation", "post_handback", "inherited"],
)
def test_source_supervisor_retains_actual_fixture_fd_and_never_blindly_relaunches(
    tmp_path, monkeypatch, fault, continuation
):
    from coire_node import expired_recovery_cold_supervision as worker

    def exercise(settings, owner, bootstrap, run, handback, control):
        if continuation:
            now = handback.observed_at + timedelta(seconds=1)

            class Clock(datetime):
                @classmethod
                def now(cls, tz=UTC):
                    return now

            monkeypatch.setattr(__import__(__name__), "datetime", Clock)
            monkeypatch.setattr(worker, "datetime", Clock)
        control.accept_cold_recovery_handback_receipt = Mock(side_effect=lambda value: value)
        monkeypatch.setattr(worker, "verify_fixture_supervisor", lambda *_: None)
        original_write = PrivateUpgradeStore.write
        lost = False

        def write(store, key, value, **kw):
            nonlocal lost
            result = original_write(store, key, value, **kw)
            expected = {
                "intent": "cold.supervision.intent.json",
                "receipt": "cold.supervision.receipt.json",
                "launch": "cold.supervision.launch.json",
            }.get(fault)
            if key == expected and not lost:
                lost = True
                raise UpgradeUnavailable("fixture lost supervisor response")
            return result

        calls = []

        def spawn(argv, **kw):
            calls.append(argv)
            assert argv == [
                str(tmp_path / "graphs/envs/target/bin/python3"),
                "-I",
                "-B",
                "-m",
                "coire_node.expired_recovery_source_supervisor",
            ]
            assert kw["close_fds"] and kw["start_new_session"]
            (descriptor,) = kw["pass_fds"]
            assert kw["env"]["UPGRADE_RECOVERY_SUPERVISION_FD"] == str(descriptor)
            records = PrivateUpgradeStore(bootstrap.root.parent / "fixture-records", create=False)
            actual = records.path("fixture-supervisor.lock").stat()
            held = os.fstat(descriptor)
            assert (held.st_dev, held.st_ino) == (actual.st_dev, actual.st_ino)
            with pytest.raises(UpgradeConflict), records.ownership("fixture-supervisor.lock"):
                pass
            assert run.path("cold.supervision.launch.json").exists()
            from coire_core.models.upgrade_recovery import (
                UpgradeExpiredRecoveryColdSupervisionIntent,
            )

            intent = run.read(
                "cold.supervision.intent.json", UpgradeExpiredRecoveryColdSupervisionIntent
            )
            if fault == "startup":
                monkeypatch.setattr(
                    worker,
                    "datetime",
                    SimpleNamespace(now=lambda _: intent.started_at + timedelta(seconds=31)),
                )
                assert intent.started_at + timedelta(seconds=31) < owner.intent.deadline_at
                return SimpleNamespace(pid=98767)
            receipt = UpgradeExpiredRecoveryColdSupervisionReceipt(
                supervision=intent,
                process=NodeAgentProcessIdentity(
                    pid=98767,
                    create_time_s=datetime.now(UTC).timestamp(),
                    environment_sha256=owner.runtime.environment_sha256,
                ),
                kind="fixture",
                observed_at=datetime.now(UTC),
            )
            run.write("cold.supervision.receipt.json", receipt, replace=False)
            return SimpleNamespace(pid=98767)

        monkeypatch.setattr(worker.subprocess, "Popen", spawn)
        monkeypatch.setattr(PrivateUpgradeStore, "write", write)
        if fault == "revoked":
            control.accept_cold_recovery_handback_receipt.side_effect = UpgradeUnavailable(
                "revoked"
            )
            with pytest.raises(UpgradeUnavailable, match="revoked"):
                worker.supervise_cold_source(settings, handback, control)
            assert calls == []
            assert not run.path("cold.supervision.intent.json").exists()
            return
        if fault == "startup":
            for _ in range(2):
                with pytest.raises(UpgradeUnavailable, match="unresolved"):
                    worker.supervise_cold_source(settings, handback, control)
            assert len(calls) == 1
            assert not run.path("cold.supervision.receipt.json").exists()
            return
        if fault is not None:
            with pytest.raises(UpgradeUnavailable, match="lost supervisor"):
                worker.supervise_cold_source(settings, handback, control)
        if fault == "launch":
            # A marker with no acknowledgement is never interpreted as an
            # unexecuted command. Expiry refuses without a second Popen.
            monkeypatch.setattr(
                worker, "datetime", SimpleNamespace(now=lambda _: owner.intent.deadline_at)
            )
            with pytest.raises(UpgradeUnavailable, match="unresolved"):
                worker.supervise_cold_source(settings, handback, control)
            assert calls == []
            return
        result = worker.supervise_cold_source(settings, handback, control)
        assert worker.supervise_cold_source(settings, handback, control) == result
        assert len(calls) == 1
        assert result.supervision.handback == handback
        assert (
            run.read("cold.supervision.receipt.json", UpgradeExpiredRecoveryColdSupervisionReceipt)
            == result
        )
        with pytest.raises(UpgradeConflict), bootstrap.ownership("recovery-transfer.lock"):
            pass

    perform_source_handback_fixture(
        tmp_path,
        monkeypatch,
        callback=exercise,
        supervision="fixture",
        continuation=continuation,
        post_handback=continuation == "post_handback",
        inherited=continuation == "inherited",
    )
