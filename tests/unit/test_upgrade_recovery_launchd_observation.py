"""Only a positive fixed launchd service/PID match identifies managed ownership."""

import pytest
from test_upgrade_expired_recovery_transfer import transfer_payload

from coire_core.errors import UpgradeUnavailable


@pytest.mark.parametrize("fault", [None, "absent", "ambiguous", "malformed", "group"])
def test_fixed_launchd_observation_requires_positive_unique_owner(tmp_path, fault):
    from coire_node.expired_recovery_launchd import observe_legacy_guard_service

    guard = transfer_payload(tmp_path)["legacy_guard"]
    calls = []

    def probe(target):
        calls.append(target)
        if fault == "absent":
            return None
        if fault == "ambiguous" or target.startswith("system/"):
            return f"state = running\npid = {guard.process.pid}\nproperties = keepalive | runatload{' | abandon process group' if fault != 'group' else ''}\n".encode()
        return None

    if fault == "malformed":

        def probe(target):
            calls.append(target)
            return b"pid = unknown\n"

    if fault:
        with pytest.raises(UpgradeUnavailable):
            observe_legacy_guard_service(guard, 501, probe)
    else:
        value = observe_legacy_guard_service(guard, 501, probe)
        assert value.target == "system/com.coire.upgrade-guard"
        assert value.process == guard.process
    assert set(calls) <= {"system/com.coire.upgrade-guard", "gui/501/com.coire.upgrade-guard"}
