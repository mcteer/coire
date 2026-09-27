"""The lean profile retains real metrics and alert rules without history containers."""

from __future__ import annotations

import os
import subprocess

import httpx
import pytest
from conftest import API_URL, PROJECT

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1", reason="requires isolated Compose"
    ),
]


def test_baseline_health_and_alert_rules_are_live(admin_headers: dict[str, str]) -> None:
    response = httpx.get(f"{API_URL}/health", headers=admin_headers, timeout=5)
    assert response.status_code == 200
    services = {item["name"]: item for item in response.json()["services"]}
    assert services["prometheus"]["healthy"] is True
    assert services["alertmanager"]["healthy"] is True
    probe = (
        "import json,urllib.request;"
        "data=json.load(urllib.request.urlopen('http://prometheus:9090/api/v1/rules',timeout=3));"
        "print(','.join(sorted(r['name'] for g in data['data']['groups'] for r in g['rules']"
        " if r.get('type')=='alerting')))"
    )
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            f"{PROJECT}_coire-telemetry",
            "--entrypoint",
            "/app/.venv/bin/python3",
            "coire-migrate:ci",
            "-c",
            probe,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    names = set(result.stdout.strip().split(","))
    assert {
        "CoireSchedulerWorkersMissing",
        "CoireRunKillSlow",
        "CoireConsoleSnapshotChurn",
    } <= names
    running = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={PROJECT}",
            "--format",
            '{{.Label "com.docker.compose.service"}}',
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert {"loki", "tempo", "grafana"}.isdisjoint(running)
