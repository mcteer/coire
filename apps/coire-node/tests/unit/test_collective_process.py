"""Real local child cleanup; no engine, model, remote host or fabricated census."""

import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from coire_core.errors import UpgradeUnavailable
from coire_node.collective_process import run_collective_launcher


def test_completed_collective_retains_actual_exit_and_output() -> None:
    result = run_collective_launcher(
        [sys.executable, "-c", "print('actual output')"],
        timeout=3,
        env={"PATH": os.environ["PATH"]},
    )
    assert result.returncode == 0 and result.stdout == b"actual output\n"


@pytest.mark.parametrize("ignore_termination", [False, True])
def test_timeout_stops_owned_launcher_and_inherited_child(
    tmp_path: Path,
    ignore_termination: bool,
) -> None:
    record = tmp_path / "child"
    script = tmp_path / "launcher.py"
    child_code = (
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
        if ignore_termination
        else "import time;time.sleep(60)"
    )
    script.write_text(
        "import subprocess,sys,time\n"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "open(sys.argv[1],'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    try:
        argv = [sys.executable, str(script), str(record)]
        if ignore_termination:
            with pytest.raises(UpgradeUnavailable, match="active descendants"):
                run_collective_launcher(argv, timeout=2, env={"PATH": os.environ["PATH"]})
            return
        result = run_collective_launcher(argv, timeout=2, env={"PATH": os.environ["PATH"]})
        assert result.returncode != 0
        assert time.monotonic() - started < 2.5
        pid = int(record.read_text())
        end = time.monotonic() + 1
        while time.monotonic() < end:
            if not psutil.pid_exists(pid):
                break
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                break
            time.sleep(0.01)
        else:
            pytest.fail("Inherited child survived collective timeout")
    finally:
        # Assertion failures must not leak this explicitly owned test child.
        if record.exists():
            try:
                child = psutil.Process(int(record.read_text()))
                if child.status() != psutil.STATUS_ZOMBIE:
                    assert child.cmdline() == [sys.executable, "-c", child_code]
                    child.kill()  # Exact owned fixture child, including SIGTERM-ignore case.
            except psutil.NoSuchProcess:
                pass


@pytest.mark.parametrize("timeout", [0, -1])
def test_invalid_budget_never_launches(timeout: float) -> None:
    with pytest.raises(ValueError):
        run_collective_launcher([sys.executable], timeout=timeout, env={})


def test_live_authority_revocation_cleans_only_owned_launcher_group(tmp_path: Path) -> None:
    record = tmp_path / "started"
    script = tmp_path / "revoked.py"
    script.write_text("import sys,time;open(sys.argv[1],'w').write('started');time.sleep(60)")
    checks = 0

    def authority() -> None:
        nonlocal checks
        checks += 1
        if record.exists():
            raise UpgradeUnavailable("fixture actor revoked")

    started = time.monotonic()
    result = run_collective_launcher(
        [sys.executable, str(script), str(record)],
        timeout=3,
        env={"PATH": os.environ["PATH"]},
        authority=authority,
    )
    assert checks >= 3 and result.returncode != 0
    assert time.monotonic() - started < 3


def test_revoked_authority_before_spawn_starts_no_process(tmp_path: Path) -> None:
    record = tmp_path / "never"

    def authority() -> None:
        raise UpgradeUnavailable("fixture revoked before spawn")

    with pytest.raises(UpgradeUnavailable, match="before spawn"):
        run_collective_launcher(
            [
                sys.executable,
                "-c",
                "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('bad')",
                str(record),
            ],
            timeout=3,
            env={"PATH": os.environ["PATH"]},
            authority=authority,
        )
    assert not record.exists()
