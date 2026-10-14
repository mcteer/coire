"""Prove host/container atomic exclusion and local prepared image identities."""

import hashlib
import json
import os
import re
import runpy
import subprocess
from pathlib import Path

import pytest

from coire_core.deploy import deployment_ownership
from coire_core.models.upgrades import DeploymentOwnership
from coire_core.upgrade_journal import PrivateUpgradeStore

pytestmark = pytest.mark.integration


def test_preparation_verifies_actual_local_arm64_image() -> None:
    from upgrade_fixtures import core_plan

    reference = os.environ.get("COIRE_UPGRADE_LOCK_TEST_IMAGE", "coire-api:ci")
    image_id = subprocess.run(
        ["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
        check=True,
        timeout=10,
        capture_output=True,
        text=True,
    ).stdout.strip()
    plan = core_plan()
    services = []
    for service in plan.target.manifest.services:
        configuration = {**service.configuration, "image": image_id}
        services.append(
            service.model_copy(
                update={
                    "image_id": image_id,
                    "configuration": configuration,
                    "configuration_sha256": hashlib.sha256(
                        json.dumps(
                            configuration,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                }
            )
        )
    manifest = plan.target.manifest.model_copy(update={"services": tuple(services)})
    release = plan.target.model_copy(
        update={"manifest": manifest, "manifest_sha256": manifest.sha256()}
    )
    script = runpy.run_path("scripts/coire-deploy.py")
    script["verify_upgrade_images"](release)


LOCK_PROBE = """import os
try:
    fd = os.open('/coordination/deployment.lease', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
except FileExistsError:
    print('blocked')
else:
    os.close(fd)
    os.unlink('/coordination/deployment.lease')
    print('acquired')
"""


def test_actual_host_lock_excludes_docker_container_namespace(tmp_path: Path) -> None:
    reference = os.environ.get("COIRE_UPGRADE_LOCK_TEST_IMAGE", "coire-api:ci")
    pinned = subprocess.run(
        ["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
        check=True,
        timeout=10,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", pinned)
    store = PrivateUpgradeStore(tmp_path / "coordination")
    argv = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "32",
        "--memory",
        "256m",
        "--cpus",
        "0.5",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--mount",
        f"type=bind,source={store.root},target=/coordination",
        pinned,
        "-B",
        "-c",
        LOCK_PROBE,
    ]
    claim = DeploymentOwnership(
        project="coire-test",
        owner_kind="host",
        owner_identity="host-test",
        boot_identity="test-boot",
        generation=1,
    )
    with deployment_ownership(store.root, claim):
        blocked = subprocess.run(argv, check=True, timeout=30, capture_output=True, text=True)
        assert blocked.stdout.strip() == "blocked", (
            "Docker filesystem does not enforce the shared atomic ownership claim"
        )
    available = subprocess.run(argv, check=True, timeout=30, capture_output=True, text=True)
    assert available.stdout.strip() == "acquired"
