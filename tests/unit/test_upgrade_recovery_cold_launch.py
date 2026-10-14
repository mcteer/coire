"""Fixed Source spawn, lost response reconciliation, and duplicate refusal."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_cold_source import source_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdSourceProcess
from coire_core.models.upgrades import UpgradeInventoryEntry
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_lease import bind_recovery_lease


@pytest.mark.parametrize(
    "fault", [None, "lost_receipt", "spawn_uncertain", "duplicate", "foreign_argv", "revoked"]
)
def test_cold_source_fixed_launch_never_blindly_retries(tmp_path, monkeypatch, fault):
    monkeypatch.setenv("UPGRADE_RECOVERY_CORE_API_PORT", "48519")
    monkeypatch.setenv("CORE_API_PORT", "39300")
    from coire_node import expired_recovery_cold_launch as launcher
    from coire_node import expired_recovery_cold_owner as physical

    selector, settings, claim, bootstrap, run, _, _, control = source_fixture(tmp_path, monkeypatch)
    children = []
    python = str(tmp_path / "graphs/envs/source/bin/python3")
    native_child = Mock()
    native_child.cmdline.return_value = [python, "-I", "-B", "-m", "coire_node"]

    def spawn(argv, **kwargs):
        assert argv == [python, "-I", "-B", "-m", "coire_node"]
        assert kwargs["close_fds"] and kwargs["start_new_session"]
        assert "UPGRADE_RECOVERY_CORE_API_PORT" not in kwargs["env"]
        assert kwargs["env"]["CORE_API_PORT"] == "39300"
        assert run.path("cold.source.launch.0.json").exists()
        if fault == "spawn_uncertain":
            raise OSError("fixture lost process creation response")
        birth = datetime.now(UTC).timestamp()
        children.append(
            UpgradeInventoryEntry(
                owner="cold-recovery",
                kind="native-process",
                work_id="pid-98765",
                state="active",
                process_id=98765,
                process_created_at=birth,
            )
        )
        if fault == "duplicate":
            children.append(children[0].model_copy(update={"process_id": 98766}))
        if fault == "foreign_argv":
            native_child.cmdline.return_value = [
                str(tmp_path / "graphs/envs/target/bin/python3"),
                "-I",
                "-B",
                "-m",
                "coire_node",
            ]
        return SimpleNamespace(pid=98765)

    popen = Mock(side_effect=spawn)
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(
        launcher.psutil,
        "Process",
        lambda _: SimpleNamespace(create_time=lambda: children[0].process_created_at),
    )
    for module in (launcher, physical):
        monkeypatch.setattr(module, "native_process_entries", lambda *_: tuple(children))
        monkeypatch.setattr(module, "_owned_node_process", lambda *_: native_child)
    original_write = PrivateUpgradeStore.write
    lost = False

    def write(store, key, value, **kwargs):
        nonlocal lost
        result = original_write(store, key, value, **kwargs)
        if fault == "lost_receipt" and key == "cold.source.process.0.json" and not lost:
            lost = True
            raise UpgradeUnavailable("fixture lost receipt acknowledgement")
        return result

    monkeypatch.setattr(PrivateUpgradeStore, "write", write)
    with (
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
        bind_recovery_lease(bootstrap, claim.sha256(), descriptor),
    ):
        source = selector.project_cold_source(settings, claim, control)
        if fault == "revoked":
            control.verify_cold_recovery_owner.side_effect = UpgradeUnavailable("fixture revoked")
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                launcher.launch_cold_source(settings, source, control)
            if fault != "lost_receipt":
                with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                    launcher.launch_cold_source(settings, source, control)
        if fault in {None, "lost_receipt"}:
            receipt = launcher.launch_cold_source(settings, source, control)
            assert launcher.launch_cold_source(settings, source, control) == receipt
            assert (
                run.read("cold.source.process.0.json", UpgradeExpiredRecoveryColdSourceProcess)
                == receipt
            )
            assert receipt.process.pid == 98765
    assert popen.call_count == (0 if fault == "revoked" else 1)


@pytest.mark.parametrize("fault", [None, "foreign_helper", "unknown_guard"])
def test_fresh_source_discovery_distinguishes_its_recorded_handback_guard(
    tmp_path, monkeypatch, fault
):
    from test_upgrade_recovery_cold_handback import perform_source_handback_fixture

    from coire_node import expired_recovery_cold_launch as launcher

    def inspect(settings, owner, bootstrap, run, receipt, control):
        source = receipt.helper.handback.verification.request.smoke.smoke.source
        python = str(tmp_path / "graphs/envs/source/bin/python3")
        identities = [source.process, receipt.helper.process]
        if fault == "unknown_guard":
            identities.append(receipt.helper.process.model_copy(update={"pid": 98767}))
        entries = tuple(
            UpgradeInventoryEntry(
                owner="cold-recovery",
                kind="native-process",
                work_id=f"pid-{p.pid}",
                state="active",
                process_id=p.pid,
                process_created_at=p.create_time_s,
            )
            for p in identities
        )
        monkeypatch.setattr(launcher, "native_process_entries", lambda *_: entries)

        def node(identity, executable):
            if identity != source.process:
                raise UpgradeUnavailable("Unknown actor is not the recorded Source node")
            child = Mock()
            child.cmdline.return_value = [python, "-I", "-B", "-m", "coire_node"]
            return child

        monkeypatch.setattr(launcher, "_owned_node_process", node)
        guard = Mock()
        guard.cmdline.return_value = [python, "-I", "-B", "-m", "coire_node.upgrade_guard"]
        if fault == "foreign_helper":
            guard.cmdline.return_value = [python, "-m", "foreign_module"]
        monkeypatch.setattr(launcher, "_owned_native_process", lambda *_: guard)
        if fault:
            with pytest.raises(UpgradeUnavailable):
                launcher.observed_source_process(settings, source.launch)
        else:
            assert launcher.observed_source_process(settings, source.launch) == source.process

    perform_source_handback_fixture(tmp_path, monkeypatch, callback=inspect)
