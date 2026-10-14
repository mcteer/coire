"""Launch boundaries are durable; native identity facts are explicit fixtures."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySupportStartInput,
    UpgradeExpiredRecoverySupportStartup,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("fault", [None, "intent", "receipt", "spawn", "revoked", "dead"])
def test_fixed_support_launch_never_repeats_unknown_effect(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_startup as worker

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    body = UpgradeExpiredRecoverySupportStartInput(
        intent=owner.consumption.transfer.intent, runtime=owner.runtime
    )
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    runs = PrivateUpgradeStore(root.path("expired-recovery"))
    run = PrivateUpgradeStore(runs.path(body.intent.recovery_id))
    python = tmp_path / "envs/support/bin/python3"
    settings = Settings(upgrades_enabled=True, upgrade_bootstrap_root=str(root.root))
    startup = UpgradeExpiredRecoverySupportStartup(
        intent=body.intent, runtime=body.runtime, prepared_at=datetime.now(UTC)
    )
    control = SimpleNamespace(verify_support_context=Mock(return_value=body.intent))
    monkeypatch.setattr(worker, "launcher_python", lambda *_: python)
    monkeypatch.setattr(worker, "prepare_support_startup", lambda *_: startup)
    monkeypatch.setattr(worker, "_owned_native_process", lambda *_: object())
    monkeypatch.setattr(
        worker.psutil,
        "Process",
        lambda _: SimpleNamespace(create_time=lambda: datetime.now(UTC).timestamp()),
    )
    spawn = Mock(return_value=SimpleNamespace(pid=12345))
    monkeypatch.setattr(worker.subprocess, "Popen", spawn)
    original_write = PrivateUpgradeStore.write
    lost = False

    def write(store, key, value, **kw):
        nonlocal lost
        result = original_write(store, key, value, **kw)
        if key == f"support.launch.{fault}.json" and not lost:
            lost = True
            raise UpgradeUnavailable("lost effect acknowledgement")
        return result

    monkeypatch.setattr(PrivateUpgradeStore, "write", write)
    if fault == "revoked":
        control.verify_support_context.side_effect = UpgradeUnavailable("revoked")
        with pytest.raises(UpgradeUnavailable, match="revoked"):
            worker.launch_support(settings, body, control)
        assert spawn.call_count == 0
        assert not run.path("support.launch.intent.json").exists()
        return
    if fault == "spawn":
        spawn.side_effect = OSError("spawn refused")
        with pytest.raises(OSError, match="spawn refused"):
            worker.launch_support(settings, body, control)
    elif fault in {"intent", "receipt"}:
        with pytest.raises(UpgradeUnavailable, match="lost effect"):
            worker.launch_support(settings, body, control)
    else:
        receipt = worker.launch_support(settings, body, control)
        assert receipt.process.pid == 12345
    if fault in {"intent", "spawn"}:
        with pytest.raises(UpgradeUnavailable, match="uncertain"):
            worker.launch_support(settings, body, control)
        assert spawn.call_count == (0 if fault == "intent" else 1)
    elif fault == "dead":
        monkeypatch.setattr(worker, "_owned_native_process", lambda *_: None)
        with pytest.raises(UpgradeUnavailable, match="succession"):
            worker.launch_support(settings, body, control)
        assert spawn.call_count == 1
    else:
        first = worker.launch_support(settings, body, control)
        assert worker.launch_support(settings, body, control) == first
        assert spawn.call_count == 1
        args, kw = spawn.call_args
        assert args[0] == [str(python), "-I", "-B", "-m", "coire_node.upgrade_guard"]
        assert kw["env"]["UPGRADE_RECOVERY_ID"] == body.intent.recovery_id
        assert kw["env"]["HF_HUB_OFFLINE"] == "1"
        assert kw["close_fds"] and kw["start_new_session"]
    assert not root.path("claim.json").exists()
    assert not root.path("runtime.json").exists()


def test_launch_input_cannot_supply_extra_authority(tmp_path):
    from pydantic import ValidationError

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    payload = {
        "intent": owner.consumption.transfer.intent.model_dump(mode="json"),
        "runtime": owner.runtime.model_dump(mode="json"),
    }
    for key in ("argv", "environment", "path", "recovery_s", "deadline_at"):
        with pytest.raises(ValidationError):
            UpgradeExpiredRecoverySupportStartInput.model_validate(payload | {key: "untrusted"})
