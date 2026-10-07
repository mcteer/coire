"""Reusable disposable Postgres 17 support for opt-in training integration tests."""

from __future__ import annotations

import json
import secrets
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.engine import make_url


@contextmanager
def disposable_postgres() -> Iterator[str]:
    name = f"coire-016-test-{secrets.token_hex(8)}"
    password = secrets.token_urlsafe(24)
    run = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            "com.coire.test=016",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            f"POSTGRES_PASSWORD={password}",
            "--env",
            "POSTGRES_DB=training_test",
            "postgres:17-alpine",
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode:
        raise RuntimeError("cannot create isolated Postgres 17 training container")
    try:
        port = subprocess.run(
            ["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", name],
            check=True,
            capture_output=True,
            text=True,
        )
        binding = json.loads(port.stdout)["5432/tcp"][0]
        if binding["HostIp"] != "127.0.0.1":
            raise RuntimeError("training test database is not bound to loopback")
        url = make_url("postgresql+asyncpg://postgres@127.0.0.1/training_test").set(
            password=password, port=int(binding["HostPort"])
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "postgres",
                    "-d",
                    "training_test",
                ],
                capture_output=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("isolated training Postgres did not become ready within 45 seconds")
        yield url.render_as_string(hide_password=False)
    finally:
        subprocess.run(
            ["docker", "rm", "--force", "--volumes", name], check=True, capture_output=True
        )
