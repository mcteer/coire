"""Bring-up integration tests (T022).

Requires Docker and is skipped unless COIRE_INTEGRATION=1. Validates SC-001 and the
edge cases around secrets and concurrent bring-up.
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
from conftest import API_URL, POSTGRES_PASSWORD
from conftest import integration_env as env_with_secrets

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run bring-up tests",
    ),
]

REPO = Path(__file__).resolve().parents[2]
COMPOSE_DIR = REPO / "deploy/compose"
UP = COMPOSE_DIR / "coire-up"
DOWN = COMPOSE_DIR / "coire-down"
HEALTH = f"{API_URL}/health"
BRINGUP_BUDGET_S = 180.0


def get_health(timeout: float = 5.0) -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(HEALTH, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        return json.load(exc)
    except Exception:
        return None


def wait_healthy(deadline_s: float) -> dict[str, object] | None:
    end = time.time() + deadline_s
    while time.time() < end:
        body = get_health()
        if body and body.get("status") == "healthy":
            return body
        time.sleep(1)
    return None


def test_reaches_all_services_healthy_within_budget() -> None:
    """SC-001: a clean bring-up reaches all-healthy unattended."""
    body = wait_healthy(BRINGUP_BUDGET_S)
    assert body is not None, "control plane never reached healthy"
    names = {s["name"] for s in body["services"]}  # type: ignore[index,union-attr]
    assert names == {"postgres", "mcp", "scheduler", "otel-collector"}
    assert all(s["healthy"] for s in body["services"])  # type: ignore[index,union-attr]


def test_health_is_served_through_nginx_not_directly() -> None:
    """nginx is the sole ingress (FR-008); the API is not published."""
    assert get_health() is not None
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2)


def test_migrate_ran_once_and_exited_zero() -> None:
    """FR-010: migrations run in a one-shot service, not inside a long-lived one."""
    cid = subprocess.run(
        ["docker", "compose", "ps", "-aq", "coire-migrate"],
        capture_output=True,
        text=True,
        cwd=COMPOSE_DIR,
    ).stdout.strip()
    assert cid, "coire-migrate container not found"
    out = subprocess.run(
        ["docker", "inspect", cid, "--format", "{{.State.Status}} {{.State.ExitCode}}"],
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "exited 0", out.stdout


def test_missing_secret_aborts_and_starts_nothing(tmp_path: Path) -> None:
    """Spec edge case: bring-up must name the missing secret and start nothing."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COIRE_SECRET_")}
    env["COIRE_SECRETS_DIR"] = str(tmp_path / "secrets")
    proc = subprocess.run(
        [str(UP), "--secrets-from-env", "--no-build"], env=env, capture_output=True, text=True
    )
    assert proc.returncode == 2, proc.stderr
    assert "missing environment secret" in proc.stderr


def test_concurrent_bringup_is_refused() -> None:
    """Two operators must not race the migration; the second exits 3."""
    env = env_with_secrets()
    a = subprocess.Popen(
        [str(UP), "--secrets-from-env", "--no-build"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    b = subprocess.Popen(
        [str(UP), "--secrets-from-env", "--no-build"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ra, rb = a.wait(timeout=300), b.wait(timeout=300)
    codes = sorted([ra, rb])
    err = (a.stderr.read() if a.stderr else "") + (b.stderr.read() if b.stderr else "")
    assert 3 in codes, f"expected one run to be refused with exit 3, got {codes}\n{err}"
    assert "bring-up already running" in err


def test_no_secret_is_written_inside_the_repository() -> None:
    """FR-011: the secret never lands in the repo or an image."""
    secrets_dir = Path(os.environ.get("COIRE_SECRETS_DIR", Path.home() / ".coire/secrets"))
    assert not str(secrets_dir).startswith(str(REPO))
    hits = subprocess.run(
        [
            "grep",
            "-rlF",
            POSTGRES_PASSWORD,
            str(REPO),
            "--exclude-dir=.git",
            "--exclude-dir=.venv",
            "--exclude-dir=node_modules",
        ],
        capture_output=True,
        text=True,
    )
    assert hits.stdout.strip() == "", f"secret found in: {hits.stdout}"
