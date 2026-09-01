"""Restart isolation (T036) — SC-002 and SC-003.

The property under test is the one Principle II-a exists for: restarting any single service
must not disturb the others. The subtle case is coire-web, whose healthcheck must probe a
local path — probing a proxied path would make every coire-api restart mark web unhealthy.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import API_URL

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run restart tests",
    ),
]

REPO = Path(__file__).resolve().parents[2]
COMPOSE_DIR = REPO / "deploy/compose"
HEALTH = f"{API_URL}/health"
NGINX_HEALTH = f"{API_URL}/nginx-health"

# /health is served BY coire-api through nginx, so restarting either of those makes the
# endpoint itself unavailable. That is the service under test being down, not collateral
# damage, so those two are asserted on differently.
INDEPENDENT = ["coire-mcp", "coire-scheduler", "otel-collector", "docker-socket-proxy"]


def compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args], cwd=COMPOSE_DIR, capture_output=True, text=True
    )


def container_id(service: str) -> str:
    """Resolve a container through compose rather than guessing `<project>-<service>-1`."""
    return compose("ps", "-q", service).stdout.strip()


def inspect(service: str, fmt: str) -> str:
    cid = container_id(service)
    if not cid:
        return ""
    out = subprocess.run(
        ["docker", "inspect", cid, "--format", fmt], capture_output=True, text=True
    )
    return out.stdout.strip()


def container_health(service: str) -> str:
    return inspect(
        service, "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}"
    )


def health_body() -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(HEALTH, timeout=3) as resp:
            return json.load(resp)
    except Exception:
        return None


def wait_all_healthy(timeout_s: float = 90.0) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        body = health_body()
        if body and body.get("status") == "healthy":
            return True
        time.sleep(1)
    return False


@pytest.fixture(autouse=True)
def settled() -> None:
    assert wait_all_healthy(), "stack was not healthy before the test"
    yield
    assert wait_all_healthy(), "stack did not recover after the test"


@pytest.mark.parametrize("service", INDEPENDENT)
def test_restarting_one_service_leaves_the_others_healthy(service: str) -> None:
    """SC-002 for services that /health does not itself depend on."""
    compose("restart", service)
    disturbed: set[str] = set()
    deadline = time.time() + 25
    while time.time() < deadline:
        body = health_body()
        if body is None:
            disturbed.add("<health endpoint unavailable>")
        else:
            alias = {
                "coire-mcp": "mcp",
                "coire-scheduler": "scheduler",
                "otel-collector": "otel-collector",
            }.get(service)
            for s in body["services"]:  # type: ignore[index,union-attr]
                if not s["healthy"] and s["name"] != alias:
                    disturbed.add(s["name"])
        time.sleep(0.5)
    assert not disturbed, f"restarting {service} disturbed {sorted(disturbed)}"


def test_collector_outage_does_not_fail_control_plane_requests() -> None:
    """FR-017/SC-008: telemetry loss remains fail-open for serving traffic."""
    compose("stop", "otel-collector")
    try:
        statuses: list[int] = []
        for _ in range(5):
            with urllib.request.urlopen(HEALTH, timeout=3) as response:
                statuses.append(response.status)
            time.sleep(0.2)
        assert statuses == [200] * 5
    finally:
        compose("start", "otel-collector")


def test_web_stays_healthy_while_api_restarts() -> None:
    """The regression guard for the proxied-healthcheck defect.

    coire-web's healthcheck probes /nginx-health, a local `return 200`. If it probed /ready
    (proxied to coire-api) this would fail on every api restart.
    """
    compose("restart", "coire-api")
    observed: set[str] = set()
    deadline = time.time() + 30
    while time.time() < deadline:
        observed.add(container_health("coire-web"))
        time.sleep(0.4)
    assert observed <= {"healthy"}, f"coire-web left healthy during api restart: {observed}"


def test_api_reconnects_to_postgres_without_a_restart() -> None:
    """US2 scenario 2: a dependency restart must not require restarting its dependents."""
    api_started_before = inspect("coire-api", "{{.State.StartedAt}}")
    compose("restart", "postgres")
    assert wait_all_healthy(), "api did not recover after a postgres restart"
    api_started_after = inspect("coire-api", "{{.State.StartedAt}}")
    assert api_started_before == api_started_after, (
        "coire-api restarted; it should have reconnected"
    )


def test_stopping_mcp_degrades_rather_than_fails() -> None:
    """US2 scenario 3: a non-critical dependency's absence must not make the API unhealthy."""
    compose("stop", "coire-mcp")
    try:
        body = None
        deadline = time.time() + 30
        while time.time() < deadline:
            body = health_body()
            if body and body.get("status") == "degraded":
                break
            time.sleep(1)
        assert body is not None and body["status"] == "degraded", f"expected degraded, got {body}"
        mcp = next(s for s in body["services"] if s["name"] == "mcp")  # type: ignore[index,union-attr]
        assert mcp["healthy"] is False
    finally:
        compose("start", "coire-mcp")


def test_web_restart_is_under_five_seconds() -> None:
    """SC-003."""
    start = time.time()
    compose("restart", "coire-web")
    served = None
    while time.time() - start < 30:
        try:
            with urllib.request.urlopen(NGINX_HEALTH, timeout=1) as resp:
                if resp.status == 200:
                    served = time.time() - start
                    break
        except Exception:
            pass
        time.sleep(0.1)
    assert served is not None and served < 5.0, f"web took {served}s to serve again"


def test_web_has_no_route_to_postgres() -> None:
    """FR-006, using a TCP probe.

    An HTTP probe cannot distinguish "no route" from "not HTTP" — Postgres would fail an HTTP
    probe either way — so the check is a TCP connect, asserted to fail from web and succeed
    from api.
    """
    from_web = compose("exec", "-T", "coire-web", "/healthcheck", "--tcp", "postgres:5432")
    assert from_web.returncode != 0, "coire-web reached postgres; segmentation is broken"

    from_api = compose(
        "exec",
        "-T",
        "coire-api",
        "/app/.venv/bin/python3",
        "-c",
        "import socket;socket.create_connection(('postgres',5432),3);print('ok')",
    )
    assert from_api.returncode == 0, f"coire-api cannot reach postgres: {from_api.stderr}"
