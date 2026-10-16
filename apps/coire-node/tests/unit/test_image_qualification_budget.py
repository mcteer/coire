"""The qualification watchdog must stop native work before waiting for its return."""

import subprocess
import sys
from pathlib import Path


def test_owned_worker_exits_on_overage_and_retains_failure(tmp_path: Path) -> None:
    receipt = tmp_path / "failure.txt"
    script = r"""
import sys,time
from pathlib import Path
from coire_node import footprint
from coire_node.worker import _qualification_memory_guard
footprint.resident_bytes=lambda pid: 101
class Job:
 def fail(self, kind, reason): Path(sys.argv[1]).write_text(str(kind)+":"+reason)
with _qualification_memory_guard(Job(),100):
 time.sleep(10)
raise RuntimeError("watchdog did not stop worker")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(receipt)], capture_output=True, timeout=5
    )
    assert result.returncode == 1
    assert "memory budget exceeded" in receipt.read_text()
