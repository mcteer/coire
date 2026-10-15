"""Identity-checked custody of the private native test supervisor."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil


def owned_supervisor(root: Path, trial: str) -> psutil.Process | None:
    receipt = root / "fixture-records" / ("application-supervisor-" + trial + ".json")
    if not receipt.exists():
        return None
    assert not receipt.is_symlink() and receipt.stat().st_uid == os.getuid()
    value = json.loads(receipt.read_text())
    try:
        process = psutil.Process(value["pid"])
        if (
            process.create_time() != value["create_time"]
            or process.status() == psutil.STATUS_ZOMBIE
        ):
            return None
        assert process.uids().real == process.uids().effective == value["uid"] == os.getuid()
        args = process.cmdline()
        assert args[-2:] == [str(root), trial]
        script = Path(process.cwd()) / args[-3]
        expected = Path(__file__).with_name("application_supervisor.py").resolve(strict=True)
        assert script.resolve(strict=True) == expected == Path(value["script"])
        assert hashlib.sha256(expected.read_bytes()).hexdigest() == value["script_sha256"]
        assert Path(process.exe()).resolve(strict=True) == Path(sys.executable).resolve(strict=True)
        return process
    except psutil.NoSuchProcess:
        return None


def start_supervisor(root: Path, trial: str) -> dict[str, Any]:
    existing = owned_supervisor(root, trial)
    if existing is not None:
        return {"pid": existing.pid, "create_time": existing.create_time()}
    script = Path(__file__).with_name("application_supervisor.py").resolve(strict=True)
    log_path = root / "log" / ("supervisor-" + trial + "-" + uuid4().hex[:10] + ".log")
    with log_path.open("xb") as output:
        log_path.chmod(0o600)
        child = subprocess.Popen(
            [sys.executable, str(script), str(root), trial],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert child.poll() is None, "private supervisor exited; inspect retained log"
            observed = owned_supervisor(root, trial)
            if observed is not None and observed.pid == child.pid:
                return {"pid": observed.pid, "create_time": observed.create_time()}
            time.sleep(0.1)
        raise AssertionError("private supervisor custody was not observed")


def stop_supervisor(root: Path, trial: str) -> None:
    process = owned_supervisor(root, trial)
    if process is not None:
        process.terminate()
        process.wait(15)
        assert owned_supervisor(root, trial) is None
