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

# Generated per run, not hard-coded: the leak test greps the tree for this exact value, and a
# literal here would match its own definition rather than a real leak.
POSTGRES_PASSWORD = f"it-{secrets.token_urlsafe(24)}"

INTEGRATION_SECRETS = {
    "COIRE_SECRET_POSTGRES_PASSWORD": POSTGRES_PASSWORD,
    "COIRE_SECRET_KEY_SIGNING_SECRET": f"it-{secrets.token_urlsafe(32)}",
    "COIRE_SECRET_NODE_TOKENS": json.dumps({"coire-edge-a": "a", "coire-edge-b": "b"}),
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
    subprocess.run([str(UP), "--secrets-from-env", "--no-build"], env=env, check=True)
    try:
        yield
    finally:
        subprocess.run(
            ["docker", "compose", "-p", PROJECT, "down", "-v", "--remove-orphans"],
            cwd=COMPOSE_DIR,
            env=env,
            capture_output=True,
        )
