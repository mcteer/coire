"""Actual Darwin kernel metadata: no engines, ownership or recovery effects."""

import platform
import subprocess
import time
from datetime import UTC, datetime, timedelta

import pytest

from coire_core.errors import UpgradeUnavailable
from coire_node.recovery_identity import recovery_kernel_boot_identity

pytestmark = [pytest.mark.integration, pytest.mark.native_upgrade]


def test_actual_fixed_darwin_kernel_read_matches_system_inspection() -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("This required native check needs an Apple Silicon Mac")
    expected = (
        subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
            check=True,
            capture_output=True,
            timeout=5,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
        .stdout.decode("ascii")
        .strip()
        .lower()
    )
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    for _ in range(100):
        started = time.monotonic()
        assert recovery_kernel_boot_identity(deadline) == expected
        assert time.monotonic() - started < 1


def test_actual_expired_kernel_read_refuses_without_new_epoch() -> None:
    with pytest.raises(UpgradeUnavailable, match="outside supported epoch"):
        recovery_kernel_boot_identity(datetime.now(UTC) - timedelta(seconds=1))
