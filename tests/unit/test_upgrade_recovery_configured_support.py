"""Configured support mode uses only existing immutable startup, no fallback."""

import threading
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySupportStartup,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "missing", "corrupt"])
def test_configured_support_existing_startup_only(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_support as worker

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    intent = owner.consumption.transfer.intent
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(root.root / "expired-recovery")
    run = PrivateUpgradeStore(runs.root / intent.recovery_id)
    if fault != "missing":
        run.write(
            "startup.json",
            UpgradeExpiredRecoverySupportStartup(
                intent=intent, runtime=owner.runtime, prepared_at=owner.claimed_at
            ),
        )
    if fault == "corrupt":
        run.path("startup.json").write_text("{}")
    call = Mock()
    monkeypatch.setattr(worker, "run_support_mode", call)
    monkeypatch.setattr(
        "coire_node.expired_recovery_startup_wait.wait_for_launcher_retirement", lambda *_: True
    )
    settings = Settings(
        node_name="coire-edge-a",
        upgrades_enabled=True,
        node_control_host="127.0.0.1",
        core_control_host="core.private",
        node_token=SecretStr("fixture-token"),
        upgrade_recovery_id=intent.recovery_id,
        upgrade_bootstrap_root=str(root.root),
    )
    if fault:
        with pytest.raises(UpgradeUnavailable):
            worker.run_configured_support(settings, threading.Event())
        call.assert_not_called()
    else:
        worker.run_configured_support(settings, threading.Event())
        call.assert_called_once()
        assert call.call_args.args[1:3] == (intent, owner.runtime)
    assert not (root.root / "runtime.json").exists()


@pytest.mark.parametrize("fault", [None, "missing", "error"])
def test_configured_initial_succession_is_explicit_no_fallback(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_successor_session as session
    from coire_node import expired_recovery_support as worker

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    intent = owner.consumption.transfer.intent
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(root.path("expired-recovery"))
    run = PrivateUpgradeStore(runs.path(intent.recovery_id))
    run.write(
        "startup.json",
        UpgradeExpiredRecoverySupportStartup(
            intent=intent, runtime=owner.runtime, prepared_at=owner.claimed_at
        ),
    )
    if fault != "missing":
        run.write("owner.claim.json", owner)
    initial = Mock(side_effect=UpgradeUnavailable("fixture refused") if fault == "error" else None)
    legacy = Mock()
    monkeypatch.setattr(session, "run_initial_successor_session", initial)
    monkeypatch.setattr(worker, "run_support_mode", legacy)
    settings = Settings(
        node_name="coire-edge-a",
        upgrades_enabled=True,
        node_control_host="127.0.0.1",
        core_control_host="core.private",
        node_token=SecretStr("fixture-token"),
        upgrade_recovery_id=intent.recovery_id,
        upgrade_recovery_succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        upgrade_recovery_succession_mode="initial",
        upgrade_bootstrap_root=str(root.root),
    )
    if fault:
        with pytest.raises(UpgradeUnavailable):
            worker.run_configured_support(settings, threading.Event())
    else:
        worker.run_configured_support(settings, threading.Event())
        assert initial.call_args.args[1:3] == (owner, settings.upgrade_recovery_succession_id)
    assert initial.call_count == (0 if fault == "missing" else 1)
    legacy.assert_not_called()
    assert not run.path(f"successor.{settings.upgrade_recovery_succession_id}.claim.json").exists()
