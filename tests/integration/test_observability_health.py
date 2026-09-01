"""Live hysteresis/freshness behavior for the composed two-node topology."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run health integration tests",
    ),
]

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "deploy/compose"


def compose(*args: str) -> None:
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def node_health(api_url: str, headers: dict[str, str]) -> dict[str, object] | None:
    try:
        response = httpx.get(f"{api_url}/health", headers=headers, timeout=6)
        response.raise_for_status()
        body = response.json()
        return next(node for node in body["nodes"] if node["name"] == "coire-edge-a")
    except (httpx.HTTPError, StopIteration):
        return None


def wait_verdict(
    api_url: str, headers: dict[str, str], verdict: str, timeout_s: float
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    observed: dict[str, object] | None = None
    while time.monotonic() < deadline:
        observed = node_health(api_url, headers)
        if observed and observed["verdict"] == verdict:
            return observed
        time.sleep(1)
    pytest.fail(f"edge-a never reached {verdict}; last observation={observed}")


def test_node_failure_and_recovery_are_damped_without_false_freshness(
    api_url: str, admin_headers: dict[str, str]
) -> None:
    """SC-012/013/015: failure is damped, recovery is longer, stale-good is never reused."""
    wait_verdict(api_url, admin_headers, "healthy", 30)
    compose("stop", "node-a")
    try:
        down = wait_verdict(api_url, admin_headers, "unreachable", 30)
        assert float(down["seconds_since_heartbeat"]) > 0
        assert down["process_state_verified"] is False
        compose("start", "node-a")

        first = node_health(api_url, admin_headers)
        assert first is not None
        assert first["verdict"] != "healthy", "one success must not erase failure hysteresis"
        recovered = wait_verdict(api_url, admin_headers, "healthy", 40)
        assert recovered["fresh"] is True
    finally:
        compose("start", "node-a")
