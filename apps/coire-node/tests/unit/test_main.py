"""Isolated registration never bypasses managed startup or process ownership capture."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from coire_core.models.upgrades import NativeRuntimeSelection, PreparedNativeRuntime
from coire_core.settings import Settings


@pytest.mark.parametrize("registration_enabled", [False, True])
async def test_registration_switch_preserves_managed_startup_verification(
    monkeypatch: pytest.MonkeyPatch, registration_enabled: bool
) -> None:
    from coire_node import __main__ as main
    from coire_node import upgrade_guard

    settings = Settings(
        node_name="coire-edge-a",
        legacy_network_mode=False,
        upgrades_enabled=True,
        node_registration_enabled=registration_enabled,
        node_control_host="coire-edge-a.lab" if registration_enabled else "127.0.0.1",
    )
    selected = NativeRuntimeSelection(
        node="coire-edge-a",
        release_sha256="a" * 64,
        generation=0,
        selected_at=datetime.now(UTC),
        runtime=PreparedNativeRuntime(
            environment_id="fixture",
            environment_sha256="b" * 64,
            interpreter_id="fixture-interpreter",
            interpreter_sha256="c" * 64,
        ),
    )
    verified = Mock(return_value=selected)
    recorded = Mock()
    collector = Mock()
    registrar = Mock(start=AsyncMock(), stop=AsyncMock())
    construct = Mock(return_value=registrar)
    build = Mock(return_value=Mock())
    monkeypatch.setattr(main, "Settings", lambda: settings)
    monkeypatch.setattr(main, "load_node_secrets", Mock())
    monkeypatch.setattr(main, "configure_node_telemetry", Mock())
    monkeypatch.setattr(main, "MetricsCollector", Mock(return_value=collector))
    monkeypatch.setattr(main, "resolve_mesh_address", Mock(return_value=None))
    monkeypatch.setattr(main, "resolve_egress_address", Mock(return_value=None))
    monkeypatch.setattr(main, "Registrar", construct)
    monkeypatch.setattr(main, "build_registration_v2", build)
    monkeypatch.setattr(main, "serve", AsyncMock())
    monkeypatch.setattr(upgrade_guard, "verify_node_startup", verified)
    monkeypatch.setattr(upgrade_guard, "record_node_process", recorded)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", Mock())
    await main._run()
    verified.assert_called_once_with(settings)
    recorded.assert_called_once_with(settings, selected)
    collector.start.assert_called_once()
    collector.stop.assert_called_once()
    if registration_enabled:
        build.assert_called_once_with(settings)
        construct.assert_called_once()
        registrar.start.assert_awaited_once()
        registrar.stop.assert_awaited_once()
    else:
        build.assert_not_called()
        construct.assert_not_called()
