"""Fixed guard entrypoint never returns to legacy expired authority from support."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from coire_core.errors import UpgradeUnavailable
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", [None, "refused", "legacy"])
def test_guard_main_support_has_no_legacy_fallback(monkeypatch, fault):
    from coire_node import expired_recovery_support as support
    from coire_node import upgrade_guard as guard

    settings = Settings(
        node_name="coire-edge-a",
        upgrades_enabled=True,
        upgrade_recovery_id=None if fault == "legacy" else "01ARZ3NDEKTSV4RRFFQ69G5FAW",
        node_token=SecretStr("fixture-token"),
    )
    monkeypatch.setattr(guard, "Settings", lambda: settings)
    monkeypatch.setattr(guard, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(guard.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(guard.signal, "signal", lambda *args: None)
    monkeypatch.setattr("coire_node.otel.configure_node_telemetry", lambda *args, **kwargs: None)
    monkeypatch.setattr(guard.logging, "basicConfig", lambda **kwargs: None)
    keychain = Mock()
    monkeypatch.setattr("coire_node.keychain.read_item", keychain)
    delegated = Mock(return_value=None)
    legacy = Mock(return_value=None)
    run = Mock(side_effect=UpgradeUnavailable("fixture refusal") if fault == "refused" else None)
    execv = Mock()
    monkeypatch.setattr(guard, "retained_guard_delegate", delegated)
    monkeypatch.setattr(guard, "run_native_guard", legacy)
    monkeypatch.setattr(support, "run_configured_support", run)
    monkeypatch.setattr(guard.os, "execv", execv)
    if fault == "refused":
        with pytest.raises(SystemExit) as failed:
            guard.main()
        assert failed.value.code == 1
    else:
        guard.main()
    assert run.call_count == (0 if fault == "legacy" else 1)
    assert legacy.call_count == delegated.call_count == (1 if fault == "legacy" else 0)
    keychain.assert_not_called()
    execv.assert_not_called()
