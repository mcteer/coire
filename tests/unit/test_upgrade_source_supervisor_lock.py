"""Descriptor integers and another owner's lock cannot stand in for inheritance."""

import os

import pytest

from coire_core.errors import UpgradeUnavailable
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_source_supervisor import verify_inherited_fixture_lock


@pytest.mark.parametrize("fault", [None, "unlocked", "other_description", "foreign"])
def test_inherited_lock_is_actual_private_open_description(tmp_path, fault):
    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    records = PrivateUpgradeStore(tmp_path / "fixture-records")
    settings = Settings(node_name="coire-edge-a", upgrade_bootstrap_root=str(bootstrap.root))
    path = records.path("fixture-supervisor.lock")
    if fault == "unlocked":
        path.touch(mode=0o600)
        descriptor = os.open(path, os.O_RDWR)
        try:
            with pytest.raises(UpgradeUnavailable, match="continuous kernel exclusion"):
                verify_inherited_fixture_lock(settings, descriptor)
            # Refusal did not acquire the previously unowned descriptor.
            with records.ownership(path.name):
                pass
        finally:
            os.close(descriptor)
        return
    with records.ownership(path.name) as held:
        if fault is None:
            verify_inherited_fixture_lock(settings, held)
        else:
            target = path if fault == "other_description" else records.path("foreign.lock")
            descriptor = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                with pytest.raises(UpgradeUnavailable):
                    verify_inherited_fixture_lock(settings, descriptor)
            finally:
                os.close(descriptor)
