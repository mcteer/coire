"""Fixed service reads share one budget; permission errors are never absence."""

import os
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from coire_core.errors import UpgradeUnavailable


@pytest.mark.parametrize("fault", [None, "unloaded", "permission", "foreign"])
def test_fixed_launchctl_adapter_argv_error_and_shared_budget(monkeypatch, fault):
    from coire_node import expired_recovery_launchd as service

    calls = []

    def capture(argv, *, timeout):
        calls.append((argv, timeout))
        assert argv[:2] == ["/bin/launchctl", "print"]
        if fault == "unloaded":
            return subprocess.CompletedProcess(
                argv,
                113,
                b"",
                b'Could not find service "com.coire.upgrade-guard" in domain for system',
            )
        if fault == "permission":
            return subprocess.CompletedProcess(argv, 113, b"", b"permission denied")
        return subprocess.CompletedProcess(argv, 0, b"state = running\npid = 12345\n", b"")

    monkeypatch.setattr(service, "capture_owned_command", capture)
    probe = service.fixed_launchctl_probe(datetime.now(UTC) + timedelta(seconds=30))
    if fault == "foreign":
        with pytest.raises(UpgradeUnavailable):
            probe("system/foreign")
        assert not calls
    elif fault == "permission":
        with pytest.raises(UpgradeUnavailable):
            probe("system/com.coire.upgrade-guard")
    else:
        first = probe("system/com.coire.upgrade-guard")
        second = probe(f"gui/{os.getuid()}/com.coire.upgrade-guard")
        assert (first is None) == (fault == "unloaded")
        assert second == first
        assert 0 < calls[1][1] <= calls[0][1] <= 5
