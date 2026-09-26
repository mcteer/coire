"""Persisted PostgreSQL role recovery never discards the isolated project's rows."""

from __future__ import annotations

import os
import secrets
import subprocess

import pytest
from conftest import COMPOSE_DIR, PROJECT, SECRETS_DIR, UP, integration_env

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1", reason="requires isolated Compose"
    ),
]


def _row_count() -> int:
    container = subprocess.run(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={PROJECT}",
            "--filter",
            "label=com.docker.compose.service=postgres",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    result = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-X",
            "-At",
            "-U",
            "coire",
            "-d",
            "coire",
            "-c",
            "SELECT count(*) FROM coire_it_credential_sentinel",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def test_network_preflight_rejects_drift_and_explicit_recovery_keeps_data() -> None:
    current = SECRETS_DIR / "state" / PROJECT / "current"
    initial_release = current.resolve()
    original_env = integration_env(COMPOSE_PROJECT_NAME=PROJECT)
    original_password = original_env["COIRE_SECRET_POSTGRES_PASSWORD"]
    container = subprocess.run(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={PROJECT}",
            "--filter",
            "label=com.docker.compose.service=postgres",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "coire",
            "-d",
            "coire",
            "-c",
            "CREATE TABLE IF NOT EXISTS coire_it_credential_sentinel (id integer PRIMARY KEY)",
        ],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "coire",
            "-d",
            "coire",
            "-c",
            "INSERT INTO coire_it_credential_sentinel VALUES (1) ON CONFLICT DO NOTHING",
        ],
        capture_output=True,
        check=True,
    )
    before = _row_count()
    changed = {**original_env, "COIRE_SECRET_POSTGRES_PASSWORD": secrets.token_urlsafe(32)}
    try:
        rejected = subprocess.run(
            [str(UP), "--secrets-from-env", "--no-build"],
            cwd=COMPOSE_DIR,
            env=changed,
            capture_output=True,
            text=True,
        )
        assert rejected.returncode == 2
        assert "application-network PostgreSQL authentication failed" in rejected.stderr
        assert current.resolve() == initial_release
        assert _row_count() == before
        recovered = subprocess.run(
            [str(UP), "--secrets-from-env", "--no-build", "--recover-db-role"],
            cwd=COMPOSE_DIR,
            env=changed,
            capture_output=True,
            text=True,
        )
        assert recovered.returncode == 0, recovered.stderr[-1200:]
        assert _row_count() == before
        backups = list((SECRETS_DIR / "state" / PROJECT / "backups").glob("*.dump"))
        assert backups and all(path.stat().st_size > 0 for path in backups)
    finally:
        restored = subprocess.run(
            [str(UP), "--secrets-from-env", "--no-build", "--recover-db-role"],
            cwd=COMPOSE_DIR,
            env={**original_env, "COIRE_SECRET_POSTGRES_PASSWORD": original_password},
            capture_output=True,
            text=True,
        )
        assert restored.returncode == 0, restored.stderr[-1200:]
    assert _row_count() == before
