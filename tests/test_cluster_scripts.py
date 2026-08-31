from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "deploy/cluster/scripts"


def test_mutating_scripts_require_explicit_apply() -> None:
    for name in ("apply-fabrics.sh", "rollback-fabrics.sh"):
        result = subprocess.run([str(SCRIPTS / name)], capture_output=True, text=True)
        assert result.returncode == 2
        assert "explicit --apply required" in result.stderr


def test_check_modes_name_only_expected_hosts() -> None:
    rollback = subprocess.run(
        [str(SCRIPTS / "rollback-fabrics.sh"), "--check"], capture_output=True, text=True
    )
    assert rollback.returncode == 0
    assert "coire-core coire-edge-a coire-edge-b" in rollback.stdout
    assert "database untouched" in rollback.stdout


def test_scripts_have_strict_shell_mode() -> None:
    for path in SCRIPTS.glob("*.sh"):
        assert "set -euo pipefail" in path.read_text(), path
