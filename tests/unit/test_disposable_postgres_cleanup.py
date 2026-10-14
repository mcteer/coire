"""Bounded fixture ownership and cleanup, including failed test bodies/startup."""

import json
import os
import subprocess
from typing import Any

import psutil
import pytest
from training_postgres import disposable_postgres


@pytest.mark.parametrize("failure", [None, "body", "inspect"])
def test_test_database_is_bounded_owned_and_removed(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    monkeypatch.delenv("COIRE_TEST_POSTGRES_BIN", raising=False)
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1] == "inspect":
            if failure == "inspect":
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "12345"}]}),
            )
        return subprocess.CompletedProcess(command, 0, "fixture-container-id\n")

    monkeypatch.setattr(subprocess, "run", run)

    def exercise() -> None:
        with disposable_postgres() as url:
            assert "127.0.0.1:12345/training_test" in url
            if failure == "body":
                raise ValueError("failed test body")

    if failure == "body":
        with pytest.raises(ValueError, match="failed test body"):
            exercise()
    elif failure == "inspect":
        with pytest.raises(subprocess.CalledProcessError):
            exercise()
    else:
        exercise()
    start = commands[0]
    name = start[start.index("--name") + 1]
    assert name.startswith("coire-test-postgres-")
    assert "com.coire.test=postgres" in start
    assert f"com.coire.test.owner_pid={os.getpid()}" in start
    assert f"com.coire.test.owner_birth={psutil.Process().create_time()}" in start
    assert "--memory=512m" in start and "--pids-limit=128" in start
    assert "--restart=no" in start
    assert start[start.index("--publish") + 1] == "127.0.0.1::5432"
    assert commands[-1] == ["docker", "rm", "--force", "--volumes", name]
