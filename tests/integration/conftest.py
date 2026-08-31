"""Shared fixtures for the integration suite.

The stack is brought up **once per session**, not per module. It was previously created by a
module-scoped fixture in test_bringup.py, which tore it down when that module finished — so
test_restart_isolation.py, which only asserts the stack is healthy, ran against nothing and
spent 90s per test waiting for a control plane that no longer existed. Running the two files
separately hid it; running the suite as CI does exposed it.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMPOSE_DIR = REPO / "deploy/compose"
UP = COMPOSE_DIR / "coire-up"
DOWN = COMPOSE_DIR / "coire-down"

# The integration suite runs in its own compose project so it never shares a Postgres volume
# with a developer's running stack. Postgres sets its password only on first init, so a volume
# created with different credentials makes coire-migrate fail to authenticate — which is
# exactly what happened locally while CI, always starting fresh, stayed green.
PROJECT = "coire-it"
os.environ.setdefault("COMPOSE_PROJECT_NAME", PROJECT)

# `coire-up` runs `docker compose up` from deploy/compose and relies on auto-discovery, so the
# integration overlay is selected with COMPOSE_FILE rather than by teaching the script a flag
# it would only ever use here. Docker Compose reads it as a path-separated list.
os.environ.setdefault("COMPOSE_FILE", f"compose.yaml{os.pathsep}compose.override.it.yaml")
# Production publishes nginx on 8180 so it can coexist with other local control planes. The
# integration contract predates that host-port choice and all live fixtures intentionally use
# loopback 8080; pin the test project explicitly instead of inheriting deploy/compose/.env.
os.environ.setdefault("COIRE_CONTROL_BIND_ADDRESS", "127.0.0.1")
os.environ.setdefault("COIRE_CONTROL_PORT", "8080")

# Generated per run, not hard-coded: the leak test greps the tree for this exact value, and a
# literal here would match its own definition rather than a real leak.
POSTGRES_PASSWORD = f"it-{secrets.token_urlsafe(24)}"

# The admin bearer for this run (ADR-0004). Generated, not fixed, for the same reason as the
# password above: the leak test greps the tree for the literal value.
ADMIN_TOKEN = f"it-admin-{secrets.token_urlsafe(24)}"
NODE_TOKEN_A = f"it-node-a-{secrets.token_urlsafe(16)}"
NODE_TOKEN_B = f"it-node-b-{secrets.token_urlsafe(16)}"

INTEGRATION_PORT = os.environ.get("COIRE_IT_PORT", "18080")
INTEGRATION_API_PORT = os.environ.get("COIRE_IT_API_PORT", "18081")
API_URL = f"http://127.0.0.1:{INTEGRATION_PORT}"
DIRECT_API_URL = f"http://127.0.0.1:{INTEGRATION_API_PORT}"

# The integration overlay adds two node agents on a simulated mesh (research R9). Feature 000's
# suite ran against compose.yaml alone; feature 001 needs nodes to exist at all.
OVERRIDE = COMPOSE_DIR / "compose.override.it.yaml"

INTEGRATION_SECRETS = {
    "COIRE_SECRET_POSTGRES_PASSWORD": POSTGRES_PASSWORD,
    "COIRE_SECRET_KEY_SIGNING_SECRET": f"it-{secrets.token_urlsafe(32)}",
    "COIRE_SECRET_ADMIN_TOKEN": ADMIN_TOKEN,
    "COIRE_SECRET_NODE_TOKENS": json.dumps(
        {"coire-edge-a": NODE_TOKEN_A, "coire-edge-b": NODE_TOKEN_B}
    ),
    "COIRE_IT_NODE_TOKEN_A": NODE_TOKEN_A,
    "COIRE_IT_NODE_TOKEN_B": NODE_TOKEN_B,
    "COIRE_IT_PORT": INTEGRATION_PORT,
    "COIRE_IT_API_PORT": INTEGRATION_API_PORT,
}


def integration_env(**extra: str) -> dict[str, str]:
    return {**os.environ, **INTEGRATION_SECRETS, **extra}


@pytest.fixture(scope="session", autouse=True)
def stack() -> Iterator[None]:
    """Bring the control plane up for the whole session and tear it down at the end."""
    if os.environ.get("COIRE_INTEGRATION") != "1":
        yield
        return

    env = integration_env(COMPOSE_PROJECT_NAME=PROJECT)
    # Start from nothing: a leftover volume carries the previous run's credentials.
    subprocess.run(
        ["docker", "compose", "-p", PROJECT, "down", "-v", "--remove-orphans"],
        cwd=COMPOSE_DIR,
        env=env,
        capture_output=True,
    )
    up = subprocess.run(
        [str(UP), "--secrets-from-env", "--no-build"],
        env=env,
        capture_output=True,
        text=True,
    )
    if up.returncode != 0:
        # Dump everything needed to diagnose without a second CI run.
        logs = subprocess.run(
            ["docker", "compose", "-p", PROJECT, "logs", "--no-color", "--tail=60"],
            cwd=COMPOSE_DIR,
            env=env,
            capture_output=True,
            text=True,
        )
        ps = subprocess.run(
            ["docker", "compose", "-p", PROJECT, "ps", "-a"],
            cwd=COMPOSE_DIR,
            env=env,
            capture_output=True,
            text=True,
        )
        pytest.fail(
            "coire-up failed (exit "
            f"{up.returncode})\n--- stdout ---\n{up.stdout}\n--- stderr ---\n{up.stderr}"
            f"\n--- ps ---\n{ps.stdout}\n--- logs ---\n{logs.stdout}",
            pytrace=False,
        )
    try:
        yield
    finally:
        if os.environ.get("COIRE_IT_KEEP_STACK") != "1":
            subprocess.run(
                ["docker", "compose", "-p", PROJECT, "down", "-v", "--remove-orphans"],
                cwd=COMPOSE_DIR,
                env=env,
                capture_output=True,
            )


@pytest.fixture(scope="session")
def api_url() -> str:
    return API_URL


@pytest.fixture(scope="session")
def direct_api_url() -> str:
    """Test-only loopback route that exposes ASGI disconnects without nginx mediation."""
    return DIRECT_API_URL


@pytest.fixture(scope="session")
def admin_headers() -> dict[str, str]:
    """The ADR-0004 admin bearer for this run."""
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture(scope="session")
def node_tokens() -> dict[str, str]:
    return {"coire-edge-a": NODE_TOKEN_A, "coire-edge-b": NODE_TOKEN_B}
