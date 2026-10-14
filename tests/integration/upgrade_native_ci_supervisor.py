"""Ephemeral hosted-CI process owner for installed node and recovery wheels.

This replaces launchd only inside the isolated test fixture. Production node and
guard modules still select runtimes and reconcile accepted operation authority.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integration.provision_upgrade_ci_vm import require_hosted_mac

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import NativeGuardStatus, UpgradeRelease
from coire_core.upgrade_journal import PrivateUpgradeStore


def supervise(root: Path, environment: dict[str, str]) -> None:
    require_hosted_mac(root)
    if (
        root.resolve(strict=True) != root
        or root.stat().st_uid != os.getuid()
        or root.stat().st_mode & 0o077
    ):
        raise RuntimeError("Native CI supervisor root is not private and owned")
    records = PrivateUpgradeStore(root / "fixture-records")
    source = records.read("source.json", UpgradeRelease)
    if source is None or environment.get("NODE_NAME") not in {"coire-edge-a", "coire-edge-b"}:
        raise RuntimeError("Native CI supervisor has no verified Source or logical node")
    envs = root / "envs"
    if environment.get("UPGRADE_ENVIRONMENT_ROOT") != str(envs):
        raise RuntimeError("Native CI supervisor cannot select another runtime root")
    source_python = envs / str(source.manifest.environment_id) / "bin/python3"
    children: dict[str, tuple[subprocess.Popen[bytes], float, BinaryIO]] = {}
    stopped = False

    def stop(signum: int, frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log_root = root / "log"
    log_root.mkdir(mode=0o700, exist_ok=True)

    def metadata(name: str, body: object) -> None:
        path = records.path(name)
        pending = records.path("metadata-" + uuid4().hex + ".pending")
        try:
            with pending.open("xb") as output:
                pending.chmod(0o600)
                output.write(json.dumps(body).encode())
                output.flush()
                os.fsync(output.fileno())
            if path.is_symlink():
                raise RuntimeError("Native CI owner metadata is linked")
            pending.replace(path)
            records.sync()
        finally:
            pending.unlink(missing_ok=True)

    def start(role: str, python: Path, module: str) -> None:
        output = (log_root / (role + ".log")).open("ab")
        process = subprocess.Popen(
            [str(python), "-I", "-B", "-m", module],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        children[role] = process, psutil.Process(process.pid).create_time(), output
        metadata(
            "fixture-processes.json",
            {
                name: {"pid": value[0].pid, "create_time": value[1]}
                for name, value in children.items()
            },
        )

    def finish(role: str) -> None:
        child, birth, output = children.pop(role)
        try:
            if child.poll() is None:
                actual = psutil.Process(child.pid)
                if actual.create_time() != birth or actual.uids().real != os.getuid():
                    raise RuntimeError("Native CI child ownership changed")
                actual.terminate()
                child.wait(timeout=15)
        finally:
            output.close()

    with records.ownership("fixture-supervisor.lock"):
        metadata(
            "fixture-supervisor.json",
            {"pid": os.getpid(), "create_time": psutil.Process().create_time()},
        )
        start("guard", source_python, "coire_node.upgrade_guard")
        try:
            while not stopped:
                if children["guard"][0].poll() is not None:
                    finish("guard")
                    start("guard", source_python, "coire_node.upgrade_guard")
                if "node" in children and children["node"][0].poll() is not None:
                    finish("node")
                current = (envs / "current").resolve(strict=True)
                if current.parent != envs:
                    raise RuntimeError("Native CI selection escaped its installed runtime root")
                try:
                    target = records.read("target.json", UpgradeRelease) or source
                    status = PrivateUpgradeStore(root / "upgrade-bootstrap").read(
                        "status.json", NativeGuardStatus
                    )
                except UpgradeUnavailable:
                    time.sleep(0.2)
                    continue
                blocked = records.path("block-candidate").exists()
                ready = (
                    status is not None
                    and status.ready
                    and 0 <= (datetime.now(UTC) - status.observed_at).total_seconds() <= 5
                )
                if (
                    "node" not in children
                    and ready
                    and not (blocked and current.name == target.manifest.environment_id)
                ):
                    start("node", current / "bin/python3", "coire_node")
                time.sleep(0.2)
        finally:
            for role in ("node", "guard"):
                if role in children:
                    finish(role)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    # Settings and credentials are supplied by the ephemeral orchestrator's env;
    # no operator Keychain or environment file is discovered by this helper.
    supervise(args.root, dict(os.environ))
