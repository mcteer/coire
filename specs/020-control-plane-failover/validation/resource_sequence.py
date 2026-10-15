"""Finish an interrupted qualification sequence without overlapping resource trials.

Adopt only an explicitly identified, same-user diagnostic runner. Wait for its
actual process exit, retain its result, then run the independent lean trial.
Results and progress stay outside Git; this never marks release tasks complete.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

HOSTS = {"coire-core", "coire-edge-a", "coire-edge-b"}
RUNNER = Path("specs/020-control-plane-failover/validation/participant_resources.py")


def result(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"qualified": False, "reason": "result_missing"}
    value = json.loads(path.read_text())
    summary = value.get("summary", {})
    telemetry = value.get("telemetry", {})
    qualified = (
        value.get("phase") == "complete"
        and set(summary) == HOSTS
        and set(telemetry.get("hosts", [])) == HOSTS
        and (not value.get("diagnostics") or telemetry.get("trace_batches", 0) > 0)
        and set(value.get("measurements", {})) == HOSTS
        and all(
            len(window) >= 2 and window[-1]["at"] - window[0]["at"] >= 900
            for windows in value.get("measurements", {}).values()
            for window in (windows.get("baseline", []), windows.get("armed", []))
        )
        and all(
            row["incremental_cpu_percent"] <= 1
            and row["incremental_rss_mib"] <= 32
            and row["swap_growth_mib"] == 0
            and row["final_role"] == ("elected" if host == "coire-core" else "standby")
            for host, row in summary.items()
        )
    )
    return {"qualified": qualified, "phase": value.get("phase"), "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostic-pid", type=int, required=True)
    parser.add_argument("--diagnostic-result", type=Path, required=True)
    parser.add_argument("--lean-result", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    args = parser.parse_args()
    assert os.uname().nodename.split(".")[0] == "coire-core"
    assert args.diagnostic_result != args.lean_result
    assert not args.lean_result.exists(), "never overwrite a prior measurement"
    process = psutil.Process(args.diagnostic_pid)
    assert process.uids().real == os.getuid()
    command = process.cmdline()
    assert str(RUNNER) in command and "--diagnostics" in command
    environment = process.environ()
    assert Path(environment["COIRE_RESOURCE_RESULT"]).resolve() == args.diagnostic_result.resolve()
    for key in ("COIRE_RESOURCE_OTEL_IMAGE", "COIRE_RESOURCE_FRONTEND_IMAGE"):
        assert "@sha256:" in environment[key]
    started = process.create_time()
    evidence: dict[str, Any] = {
        "stage": "waiting_for_diagnostic",
        "release_qualified": False,
        "review_required": True,
        "diagnostic_exit_code": None,
        "diagnostic_pid": process.pid,
        "diagnostic_created_at": started,
        "diagnostic_result": str(args.diagnostic_result),
        "lean_result": str(args.lean_result),
        "runner_sha256": hashlib.sha256(RUNNER.read_bytes()).hexdigest(),
    }

    def save() -> None:
        temporary = args.status.with_suffix(args.status.suffix + ".tmp")
        temporary.write_text(json.dumps(evidence, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(args.status)

    save()
    deadline = time.monotonic() + 3600
    while process.is_running():
        try:
            if process.status() == psutil.STATUS_ZOMBIE:
                break
            assert process.create_time() == started
        except psutil.NoSuchProcess:
            break
        assert time.monotonic() < deadline, "diagnostic runner did not finish within its bound"
        time.sleep(10)
    evidence["diagnostic"] = result(args.diagnostic_result)
    evidence["stage"] = "running_lean"
    save()
    environment["COIRE_RESOURCE_RESULT"] = str(args.lean_result)
    environment.pop("COIRE_RESOURCE_WINDOW_S", None)
    log_path = args.lean_result.with_suffix(".log")
    with log_path.open("xb") as log:
        log_path.chmod(0o600)
        completed = subprocess.run(
            [sys.executable, str(RUNNER)],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    evidence["lean_exit_code"] = completed.returncode
    evidence["lean"] = result(args.lean_result)
    evidence["stage"] = "complete"
    save()
    print(json.dumps(evidence, indent=2), flush=True)
    assert completed.returncode == 0 and evidence["diagnostic"]["qualified"]
    assert evidence["lean"]["qualified"]


if __name__ == "__main__":
    main()
