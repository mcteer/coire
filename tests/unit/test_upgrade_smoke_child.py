"""Private serving child rejects unbounded or foreign authority before engines."""

from datetime import UTC, datetime, timedelta
from io import BytesIO, StringIO
from unittest.mock import Mock

import pytest
from upgrade_fixtures import plan

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import NativeSmokeCommand, UpgradeEvidence
from coire_core.settings import Settings


def command() -> NativeSmokeCommand:
    value = plan()
    assert value.target.manifest.environment_sha256 is not None
    return NativeSmokeCommand(
        plan=value,
        runtime_sha256=value.target.manifest.environment_sha256,
        deadline_at=datetime.now(UTC) + timedelta(seconds=120),
    )


@pytest.mark.parametrize("fault", ["oversize", "unknown", "foreign", "expired"])
def test_child_refuses_bad_input_before_serving(
    fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_node import upgrade_smoke

    value = command().model_dump(mode="json")
    if fault == "unknown":
        value["path"] = "/foreign"
    elif fault == "foreign":
        value["runtime_sha256"] = "f" * 64
    elif fault == "expired":
        value["deadline_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    import json

    data = b"x" * (2 * 1024 * 1024 + 1) if fault == "oversize" else json.dumps(value).encode()
    runner = Mock()
    monkeypatch.setattr(upgrade_smoke, "run_node_smoke", runner)
    output = StringIO()
    with pytest.raises(UpgradeUnavailable):
        upgrade_smoke.smoke_child(BytesIO(data), output, Settings(node_name="coire-edge-a"))
    runner.assert_not_called()
    assert output.getvalue() == ""


def test_child_emits_only_bounded_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    from coire_node import upgrade_smoke

    value = command()
    evidence = UpgradeEvidence(
        check="node", runtime_sha256=value.runtime_sha256, cleanup_verified=True
    )
    runner = Mock(return_value=evidence)
    monkeypatch.setattr(upgrade_smoke, "run_node_smoke", runner)
    output = StringIO()
    upgrade_smoke.smoke_child(
        BytesIO(value.model_dump_json().encode()), output, Settings(node_name="coire-edge-a")
    )
    assert UpgradeEvidence.model_validate_json(output.getvalue()) == evidence
    assert len(output.getvalue().encode()) <= 4096
    client = runner.call_args.args[-1]
    assert client._trust_env is False
    assert client.follow_redirects is False


def test_core_cannot_start_smoke_by_claiming_studio_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import platform

    import httpx

    from coire_node import upgrade_smoke

    monkeypatch.setattr(platform, "node", lambda: "coire-core.local")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    engines = Mock()
    monkeypatch.setattr(upgrade_smoke, "EngineManager", engines)
    value = command()
    with httpx.Client(trust_env=False) as client, pytest.raises(UpgradeUnavailable):
        upgrade_smoke.run_node_smoke(
            Settings(node_name="coire-edge-a"),
            value.plan,
            value.runtime_sha256,
            value.deadline_at,
            client,
        )
    engines.assert_not_called()


@pytest.mark.parametrize("serving_failed", [False, True])
def test_child_main_exports_without_changing_evidence_status(
    serving_failed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal
    import sys
    from types import SimpleNamespace

    from opentelemetry import metrics, trace

    from coire_node import otel, upgrade_smoke

    settings = Settings(diagnostics_enabled=False)
    configure = Mock()
    providers = [Mock(), Mock()]
    providers[0].force_flush.side_effect = RuntimeError("export unavailable")
    child = Mock(side_effect=UpgradeUnavailable("serving failed") if serving_failed else None)
    monkeypatch.setattr(upgrade_smoke, "Settings", lambda: settings)
    monkeypatch.setattr(otel, "configure_node_telemetry", configure)
    monkeypatch.setattr(upgrade_smoke, "smoke_child", child)
    monkeypatch.setattr(signal, "signal", Mock())
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO()))
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: providers[0])
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: providers[1])
    if serving_failed:
        with pytest.raises(SystemExit) as result:
            upgrade_smoke.main()
        assert result.value.code == 1
    else:
        upgrade_smoke.main()
    configure.assert_called_once_with(
        "upgrade-smoke", settings.otlp_endpoint, diagnostics_enabled=False
    )
    for provider in providers:
        provider.force_flush.assert_called_once_with(timeout_millis=1000)
    assert child.call_args.args[-1] is settings
