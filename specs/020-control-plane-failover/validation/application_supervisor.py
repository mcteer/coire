"""Private launchd equivalent for one installed qualification node.

Follows only the verified guard selection. Never changes a pointer, grant,
maintenance fence, operation or deadline. Production launchd stays untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil

from coire_core.models.upgrades import NativeGuardStatus, NativeNodeProcess
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_guard import _owned_native_process, _owned_node_process
from coire_node.upgrade_runtime import verify_prepared_runtime


def main() -> None:
    root, trial = Path(sys.argv[1]), sys.argv[2]
    assert root.parent == Path.home() / ".coire/qualification/020"
    assert root.name.startswith("coire-020-application-")
    assert root.resolve(strict=True) == root
    assert root.stat().st_uid == os.getuid() and root.stat().st_mode & 0o077 == 0
    state = PrivateUpgradeStore(root / ("state-" + trial) / "upgrades", create=False)
    bootstrap = PrivateUpgradeStore(root / "upgrade-bootstrap", create=False)
    records = root / "fixture-records"
    records.mkdir(mode=0o700, exist_ok=True)
    stopped = False

    def stop(_signal: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    child: subprocess.Popen[bytes] | None = None
    with PrivateUpgradeStore(records).ownership("application-supervisor.lock"):
        own = psutil.Process()
        receipt = records / ("application-supervisor-" + trial + ".json")
        receipt.write_text(
            json.dumps(
                {
                    "pid": own.pid,
                    "create_time": own.create_time(),
                    "uid": os.getuid(),
                    "script": str(Path(__file__).resolve()),
                    "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                }
            )
        )
        receipt.chmod(0o600)
        status = bootstrap.read("status.json", NativeGuardStatus)
        assert status is not None
        guard_python = verify_prepared_runtime(
            root / "envs", root / "runtimes", status.bootstrap_runtime
        )
        guard = _owned_native_process(status.process, guard_python, "coire_node.upgrade_guard")
        assert guard is not None
        environment = guard.environ()
        assert environment["UPGRADE_PROJECT"] == "coire-020-application-" + trial
        assert environment["NODE_STATE_DIR"] == str(root / ("state-" + trial))
        log_path = root / "log" / ("supervised-node-" + trial + ".log")
        with log_path.open("ab") as output:
            log_path.chmod(0o600)
            while not stopped:
                if child is not None:
                    if child.poll() is None:
                        time.sleep(0.25)
                        continue
                    child.wait()
                    child = None
                    time.sleep(1)
                status = bootstrap.read("status.json", NativeGuardStatus)
                if (
                    status is None
                    or not status.ready
                    or not 0 <= (datetime.now(UTC) - status.observed_at).total_seconds() <= 5
                ):
                    time.sleep(0.25)
                    continue
                guard_python = verify_prepared_runtime(
                    root / "envs", root / "runtimes", status.bootstrap_runtime
                )
                assert (
                    _owned_native_process(status.process, guard_python, "coire_node.upgrade_guard")
                    is not None
                )
                current = state.read("node-process.json", NativeNodeProcess)
                if current is not None:
                    python = verify_prepared_runtime(
                        root / "envs", root / "runtimes", current.selected.runtime
                    )
                    if _owned_node_process(current.process, python) is not None:
                        time.sleep(0.25)
                        continue
                python = verify_prepared_runtime(
                    root / "envs", root / "runtimes", status.selected.runtime
                )
                assert (root / "envs/current").resolve(
                    strict=True
                ) == root / "envs" / status.selected.runtime.environment_id
                # A child may be starting before it publishes node-process.json.
                unknown = False
                for process in psutil.process_iter():
                    try:
                        args = process.cmdline()
                        if args[-2:] == ["-m", "coire_node"] and args[0].startswith(
                            str(root / "envs") + "/"
                        ):
                            unknown = True
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                latest = bootstrap.read("status.json", NativeGuardStatus)
                if (
                    unknown
                    or latest is None
                    or not latest.ready
                    or latest.selected != status.selected
                    or not 0 <= (datetime.now(UTC) - latest.observed_at).total_seconds() <= 5
                ):
                    time.sleep(0.25)
                    continue
                child = subprocess.Popen(
                    [str(python), "-I", "-B", "-m", "coire_node"],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                born = psutil.Process(child.pid).create_time()
                (records / ("application-child-" + trial + ".json")).write_text(
                    json.dumps(
                        {
                            "pid": child.pid,
                            "create_time": born,
                            "python": str(python),
                            "runtime_sha256": status.selected.runtime.environment_sha256,
                        }
                    )
                )
                time.sleep(0.25)
    # Stopping supervision alone never kills a selected node or clears authority.


if __name__ == "__main__":
    main()
