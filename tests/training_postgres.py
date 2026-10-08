"""Reusable disposable Postgres 17 support for opt-in training integration tests."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.engine import make_url


@contextmanager
def disposable_postgres() -> Iterator[str]:
    native = os.environ.get("COIRE_TEST_POSTGRES_BIN")
    if native:
        with native_postgres(Path(native)) as native_url:
            yield native_url
        return
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


@contextmanager
def native_postgres(binaries: Path) -> Iterator[str]:
    """Owned temporary PG17 on isolated Mac CI, with SCRAM and loopback only."""
    if not binaries.is_absolute() or not all(
        (binaries / name).is_file() for name in ("initdb", "pg_ctl", "createdb")
    ):
        raise RuntimeError("explicit test PostgreSQL binaries are missing")
    version = subprocess.check_output([str(binaries / "initdb"), "--version"], text=True)
    if "PostgreSQL) 17." not in version:
        raise RuntimeError("native test database requires PostgreSQL 17")
    with tempfile.TemporaryDirectory(prefix="coire-017-pg-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        password = secrets.token_urlsafe(24)
        password_file = root / "password"
        password_file.write_text(password)
        password_file.chmod(0o600)
        data = root / "data"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        result = subprocess.run(
            [
                str(binaries / "initdb"),
                "-D",
                str(data),
                "--username=coire_test",
                "--auth=scram-sha-256",
                "--pwfile=" + str(password_file),
            ],
            capture_output=True,
        )
        if result.returncode:
            raise RuntimeError("cannot initialize owned native test database")
        started = False
        try:
            result = subprocess.run(
                [
                    str(binaries / "pg_ctl"),
                    "-D",
                    str(data),
                    "-l",
                    str(root / "postgres.log"),
                    "-o",
                    f"-h 127.0.0.1 -p {port} -k {root}",
                    "-w",
                    "start",
                ],
                capture_output=True,
            )
            if result.returncode:
                raise RuntimeError("cannot start owned native test database")
            started = True
            environment = {**os.environ, "PGPASSWORD": password}
            result = subprocess.run(
                [
                    str(binaries / "createdb"),
                    "-h",
                    "127.0.0.1",
                    "-p",
                    str(port),
                    "-U",
                    "coire_test",
                    "training_test",
                ],
                env=environment,
                capture_output=True,
            )
            if result.returncode:
                raise RuntimeError("cannot create owned native test database")
            url = make_url("postgresql+asyncpg://coire_test@127.0.0.1/training_test").set(
                password=password, port=port
            )
            yield url.render_as_string(hide_password=False)
        finally:
            if started:
                stopped = subprocess.run(
                    [str(binaries / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"],
                    capture_output=True,
                )
                if stopped.returncode:
                    raise RuntimeError("owned native test database did not stop")
