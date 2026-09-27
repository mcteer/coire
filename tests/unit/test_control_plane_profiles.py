"""Compose profiles retain metrics and alerts while keeping diagnostics optional."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

COMPOSE = Path(__file__).resolve().parents[2] / "deploy/compose/compose.yaml"


def resolved(profiles: str = "") -> dict[str, Any]:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is unavailable")
    env = {**os.environ, "COMPOSE_PROFILES": profiles}
    env["COIRE_OTEL_CONFIG"] = "diagnostics.yaml" if "diagnostics" in profiles else "config.yaml"
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return json.loads(result.stdout)


def test_lean_baseline_has_metrics_alerts_and_resource_bounds() -> None:
    services = resolved()["services"]
    assert {"prometheus", "alertmanager", "otel-collector"} <= services.keys()
    assert {"grafana", "loki", "tempo", "coire-ops", "coire-mcp"}.isdisjoint(services)
    assert services["otel-collector"]["command"][-1].endswith("/config.yaml")
    for name, service in services.items():
        if name == "coire-migrate":
            continue
        assert service.get("mem_limit"), name
        assert service.get("pids_limit"), name
        if name not in {"coire-failover"}:
            assert service.get("logging", {}).get("options", {}).get("max-size") == "10m", name


def test_diagnostics_and_optional_capabilities_are_selected_together() -> None:
    services = resolved("diagnostics,ops,mcp")["services"]
    assert {"grafana", "loki", "tempo", "coire-ops", "coire-mcp"} <= services.keys()
    assert services["otel-collector"]["command"][-1].endswith("/diagnostics.yaml")


def test_lean_collector_has_no_disabled_exporter_destinations() -> None:
    text = (COMPOSE.parent / "otel-collector.yaml").read_text()
    assert "tempo:4317" not in text
    assert "loki:3100" not in text
    assert "endpoint: 0.0.0.0:8889" in text


def test_scheduler_uses_the_same_run_gateway_as_the_selected_deployment() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is unavailable")
    env = {
        **os.environ,
        "RUN_GATEWAY_URL": "http://gateway.example.test:8080/v1",
        "COIRE_IT_API_PORT": "18081",
    }
    base = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    assert (
        json.loads(base.stdout)["services"]["coire-scheduler"]["environment"]["RUN_GATEWAY_URL"]
        == env["RUN_GATEWAY_URL"]
    )
    overlay = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "-f",
            str(COMPOSE.with_name("compose.override.it.yaml")),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    assert (
        json.loads(overlay.stdout)["services"]["coire-scheduler"]["environment"]["RUN_GATEWAY_URL"]
        == "http://host.docker.internal:18081/v1"
    )
