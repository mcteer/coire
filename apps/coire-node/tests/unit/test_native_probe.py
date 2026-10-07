"""Real OS probes use spawn, bounded capture and a private child descriptor set."""

import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from coire_node.native_probe import run_probe


def test_probe_preserves_output_exit_status_and_deadline() -> None:
    result = run_probe(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err',file=sys.stderr); sys.exit(7)",
        ],
        timeout=5,
    )
    assert result.returncode == 7 and result.stdout == b"out\n" and result.stderr == b"err\n"
    with pytest.raises(subprocess.TimeoutExpired):
        run_probe([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)


def test_probe_closes_inheritable_descriptor_and_omits_parent_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COIRE_PROBE_TEST_SECRET", "synthetic-private-value")
    with (tmp_path / "private").open("wb") as private:
        os.set_inheritable(private.fileno(), True)
        source = f"""import os
assert 'COIRE_PROBE_TEST_SECRET' not in os.environ
try:
 os.fstat({private.fileno()})
except OSError:
 print('closed')
else:
 raise AssertionError('Parent descriptor inherited')
"""
        result = run_probe([sys.executable, "-c", source], timeout=5)
    assert result.returncode == 0 and result.stdout == b"closed\n", result.stderr


def test_darwin_probe_never_enters_python_fork_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("Probe used fork instead of native spawn")

    if platform.system() == "Darwin":
        monkeypatch.setattr(subprocess, "_fork_exec", refuse)
    assert run_probe([sys.executable, "-c", "print('spawn')"], timeout=5).stdout == b"spawn\n"


def test_probe_capture_is_bounded() -> None:
    with pytest.raises(subprocess.SubprocessError, match="bounded capture"):
        run_probe(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes(5*1024**2))"],
            timeout=5,
        )
