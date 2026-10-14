"""Access denial is not process absence; PID reuse is not old identity alive."""

from types import SimpleNamespace

import psutil
import pytest

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import NodeAgentProcessIdentity


@pytest.mark.parametrize("fault", ["gone", "alive", "denied", "reused"])
def test_retired_identity_requires_positive_absence(monkeypatch, fault):
    from coire_node import recovery_identity as worker

    identity = NodeAgentProcessIdentity(
        pid=50000, create_time_s=12345.0, environment_sha256="a" * 64
    )

    def process(pid):
        if fault == "gone":
            raise psutil.NoSuchProcess(pid)
        if fault == "denied":
            raise psutil.AccessDenied(pid)
        return SimpleNamespace(
            create_time=lambda: identity.create_time_s + (1 if fault == "reused" else 0)
        )

    monkeypatch.setattr(worker.psutil, "Process", process)
    if fault in {"denied", "alive"}:
        with pytest.raises(UpgradeUnavailable):
            worker.verify_retired_identity(identity)
    else:
        worker.verify_retired_identity(identity)
