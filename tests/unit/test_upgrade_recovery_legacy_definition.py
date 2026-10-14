"""Legacy stop needs exact installed definition and child-preservation policy."""

import plistlib
from pathlib import Path

import pytest
from test_upgrade_expired_recovery_transfer import transfer_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", [None, "group", "argv", "scope"])
def test_legacy_definition_binds_exact_service_and_state(tmp_path, fault):
    from coire_node.expired_recovery_launchd import verify_legacy_definition

    guard = transfer_payload(tmp_path)["legacy_guard"]
    settings = Settings(
        node_name=guard.node,
        upgrade_environment_root=str(tmp_path / "envs"),
        upgrade_bootstrap_root=str(tmp_path / "bootstrap"),
        node_state_dir=str(tmp_path / "state"),
    )
    document = {
        "Label": "com.coire.upgrade-guard",
        "UserName": "coire",
        "ProgramArguments": [
            str(
                Path(settings.upgrade_environment_root)
                / guard.bootstrap_runtime.environment_id
                / "bin/python3"
            ),
            "-I",
            "-B",
            "-m",
            "coire_node.upgrade_guard",
        ],
        "AbandonProcessGroup": True,
        "EnvironmentVariables": {
            "NODE_NAME": guard.node,
            "UPGRADE_ENVIRONMENT_ROOT": settings.upgrade_environment_root,
            "UPGRADE_BOOTSTRAP_ROOT": settings.upgrade_bootstrap_root,
            "NODE_STATE_DIR": settings.node_state_dir,
        },
    }
    if fault == "group":
        document["AbandonProcessGroup"] = False
    elif fault == "argv":
        document["ProgramArguments"] = ["/bin/sh"]
    elif fault == "scope":
        document["EnvironmentVariables"]["UPGRADE_BOOTSTRAP_ROOT"] = "/foreign"
    data = plistlib.dumps(document)
    if fault:
        with pytest.raises(UpgradeUnavailable):
            verify_legacy_definition(settings, guard, data, "coire")
    else:
        assert len(verify_legacy_definition(settings, guard, data, "coire")) == 64
