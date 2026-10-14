"""Exercise private host/container ownership without a gateway, socket or native engine."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from upgrade_fixtures import plan

from coire_api.upgrades.host_hold import operation_hold
from coire_core.deploy import host_boot_identity
from coire_core.models.upgrades import DeploymentOwnership
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("interrupt", [False, True], ids=["restart", "sigkill"])
def test_actual_initial_executor_restarts_live_without_inventing_bootstrap(
    tmp_path: Path, enabled: bool, interrupt: bool
) -> None:
    """Actual distroless process/health/restart, without SQL or physical Source qualification."""
    import json
    import secrets
    import time
    from uuid import uuid4

    image = os.environ.get("COIRE_UPGRADE_EXECUTOR_TEST_IMAGE", "coire-upgrade-executor:019-dev")
    name = "coire-019-first-install-" + uuid4().hex
    secret = tmp_path / "upgrade_executor_token"
    secret.write_text("coire_upgrade_" + secrets.token_hex(32))
    secret.chmod(0o600)
    user = "65532:65532" if sys.platform == "darwin" else f"{os.getuid()}:{os.getgid()}"
    assert user.split(":")[0] != "0", "fixture requires a non-root host UID"
    code = """import json
from pathlib import Path
import httpx
token=Path('/run/secrets/upgrade_executor_token').read_text().strip()
with httpx.Client(trust_env=False,timeout=2) as client:
    headers={'Authorization':'Bearer '+token}
    print(json.dumps({
        'live':client.get('http://127.0.0.1:8011/live',headers=headers).status_code,
        'ready':client.get('http://127.0.0.1:8011/ready',headers=headers).status_code,
        'anonymous':client.get('http://127.0.0.1:8011/live').status_code,
        'seed_absent':not Path('/opt/coire/upgrades/state/bootstrap/initial-seed.json').exists(),
        'selection_absent':not Path('/opt/coire/upgrades/state/bootstrap/selected-seed.json').exists(),
        'lease_absent':not Path('/opt/coire/upgrades/coordination/deployment.lease').exists(),
    }))
"""
    created = subprocess.run(
        [
            "docker",
            "create",
            "--name",
            name,
            "--pull=never",
            "--network=none",
            "--read-only",
            "--user",
            user,
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=64",
            "--memory=256m",
            "--cpus=0.5",
            "--restart=no",
            "--tmpfs=/tmp",
            "--tmpfs=/opt/coire/upgrades/coordination:uid=" + user.split(":")[0] + ",mode=0700",
            "--tmpfs=/opt/coire/upgrades/state:uid=" + user.split(":")[0] + ",mode=0700",
            "--mount",
            f"type=bind,source={secret},target=/run/secrets/upgrade_executor_token,readonly",
            "--env",
            "UPGRADES_ENABLED=" + str(enabled).lower(),
            image,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    ).stdout.strip()
    try:
        for restart in range(3):
            if restart and interrupt:
                subprocess.run(
                    ["docker", "kill", "--signal=KILL", created],
                    capture_output=True,
                    check=True,
                    timeout=15,
                )
            subprocess.run(
                ["docker", "start" if restart == 0 or interrupt else "restart", created],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            deadline = time.monotonic() + 20
            observed = None
            while True:
                probe = subprocess.run(
                    ["docker", "exec", created, "/app/.venv/bin/python3", "-B", "-c", code],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if probe.returncode == 0:
                    observed = json.loads(probe.stdout)
                    if observed["live"] == 200:
                        break
                assert time.monotonic() < deadline, (
                    "initial executor failed its bounded liveness probe; "
                    f"probe_exit={probe.returncode}, safe_status={observed}, "
                    "errors="
                    + ",".join(
                        error
                        for error in (
                            "PermissionError",
                            "ModuleNotFoundError",
                            "ImportError",
                            "ConnectionError",
                            "ConnectError",
                        )
                        if error in probe.stderr
                    )
                )
                time.sleep(0.1)
            assert observed == {
                "live": 200,
                "ready": 503,
                "anonymous": 401,
                "seed_absent": True,
                "selection_absent": True,
                "lease_absent": True,
            }
            health = subprocess.run(
                [
                    "docker",
                    "exec",
                    created,
                    "/app/.venv/bin/python3",
                    "-m",
                    "coire_upgrade_executor",
                    "--healthcheck",
                ],
                capture_output=True,
                check=False,
                timeout=5,
            )
            assert health.returncode == 0
    finally:
        subprocess.run(["docker", "rm", "-f", created], capture_output=True, check=True, timeout=15)
        secret.unlink()


def test_private_uid_mapping_and_studio_hold_block_linux_host_mutation(tmp_path: Path) -> None:
    reference = os.environ.get(
        "COIRE_UPGRADE_EXECUTOR_TEST_IMAGE", "coire-upgrade-executor:019-dev"
    )
    image = subprocess.run(
        ["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", image)
    store = PrivateUpgradeStore(tmp_path / "coordination")
    store.write(
        "ownership.json",
        DeploymentOwnership(
            project="coire",
            owner_kind="host",
            owner_identity="initial-host",
            boot_identity=host_boot_identity(),
            generation=1,
            state="committed",
            stage="selected",
        ),
    )
    settings = Settings(upgrade_coordination_dir=str(store.root))
    value = plan()
    operation_hold(settings, value)
    code = """from pathlib import Path
import time
from coire_core.deploy import deployment_mutex
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.upgrade_journal import PrivateUpgradeStore
store=PrivateUpgradeStore(Path('/coordination'))
deadline=time.monotonic()+2
while True:
    try:
        with deployment_mutex(store,'coire','isolated-linux-host'):
            print('acquired')
        break
    except UpgradeConflict:
        print('blocked')
        break
    except UpgradeUnavailable:
        if time.monotonic()>=deadline:
            raise
        time.sleep(0.05)
"""
    argv = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--user",
        # OrbStack maps private macOS binds to the service UID. Native Linux
        # binds retain host ownership, so this fixture uses its non-root host
        # UID; production uses service-owned named volumes instead of this bind.
        "65532:65532" if sys.platform == "darwin" else f"{os.getuid()}:{os.getgid()}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=32",
        "--memory=256m",
        "--cpus=0.5",
        "--mount",
        f"type=bind,source={store.root},target=/coordination",
        "--entrypoint",
        "/app/.venv/bin/python3",
        image,
        "-B",
        "-c",
        code,
    ]
    blocked = subprocess.run(argv, capture_output=True, text=True, check=True, timeout=30)
    assert blocked.stdout.strip() == "blocked"
    assert not store.path("deployment.lease").exists()
    operation_hold(settings, value, release=True)
    released = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
    assert released.returncode == 0, released.stderr
    assert released.stdout.strip() == "acquired"
    assert not store.path("deployment.lease").exists()


def test_actual_linux_writer_and_killed_owner_are_never_inferred_from_pid(tmp_path: Path) -> None:
    """Two real containers share an inode; killed intent remains fenced.

    This is CPU-only kernel/admission acceptance, not Docker/bootstrap qualification.
    """
    import time
    from uuid import uuid4

    image = os.environ.get("COIRE_UPGRADE_EXECUTOR_TEST_IMAGE", "coire-upgrade-executor:019-dev")
    store = PrivateUpgradeStore(tmp_path / "kernel-coordination")
    name = "coire-019-kernel-" + uuid4().hex
    user = "65532:65532" if sys.platform == "darwin" else f"{os.getuid()}:{os.getgid()}"
    common = [
        "--pull=never",
        "--network=none",
        "--read-only",
        "--user",
        user,
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=32",
        "--memory=256m",
        "--cpus=0.5",
        "--mount",
        f"type=bind,source={store.root},target=/coordination",
        "--entrypoint",
        "/app/.venv/bin/python3",
    ]
    holder_code = """from pathlib import Path
import time
from coire_core.deploy import deployment_ownership, host_boot_identity
from coire_core.models.upgrades import DeploymentOwnership
root=Path('/coordination')
claim=DeploymentOwnership(project='coire',owner_kind='executor',owner_identity='kernel-fixture',stage='preparing',
    executor_runtime_id='source',executor_container_id=(root/'container-id').read_text(),
    boot_identity=host_boot_identity(),generation=1,source_sha256='a'*64)
with deployment_ownership(root,claim):
    (root/'held').touch()
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        time.sleep(0.05)
    raise RuntimeError('fixture timed out without owner release')
"""
    probe_code = """from pathlib import Path
from coire_core.deploy import idle_executor_owner, deployment_mutex
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
store=PrivateUpgradeStore(Path('/coordination'))
try:
    owner=idle_executor_owner(store,'coire','kernel-fixture')
except UpgradeUnavailable:
    print('orphan-fenced')
else:
    assert owner is not None
    assert owner.executor_container_id==(store.root/'container-id').read_text()
    print('held-witness')
try:
    with deployment_mutex(store,'coire','contending-fixture'):
        raise AssertionError('contender acquired active or orphan writer')
except (UpgradeConflict, UpgradeUnavailable):
    print('contender-blocked')
"""
    identity = subprocess.run(
        ["docker", "create", "--name", name, *common, image, "-B", "-c", holder_code],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    assert re.fullmatch(r"[a-f0-9]{64}", identity)
    try:
        store.path("container-id").write_text(identity)
        subprocess.run(["docker", "start", identity], capture_output=True, check=True, timeout=10)
        deadline = time.monotonic() + 10
        while not store.path("held").exists():
            assert time.monotonic() < deadline, "isolated Linux writer failed to start"
            time.sleep(0.05)
        before = store.path("ownership.json").read_bytes()
        lease = store.path("deployment.lease").read_bytes()
        probe = ["docker", "run", "--rm", *common, image, "-B", "-c", probe_code]
        held = subprocess.run(probe, capture_output=True, text=True, check=False, timeout=10)
        assert held.returncode == 0, held.stderr
        assert held.stdout.splitlines() == ["held-witness", "contender-blocked"]
        subprocess.run(
            ["docker", "kill", "--signal=KILL", identity],
            capture_output=True,
            check=True,
            timeout=10,
        )
        subprocess.run(["docker", "wait", identity], capture_output=True, check=True, timeout=10)
        orphan = subprocess.run(probe, capture_output=True, text=True, check=False, timeout=10)
        assert orphan.returncode == 0, orphan.stderr
        assert orphan.stdout.splitlines() == ["orphan-fenced", "contender-blocked"]
        assert store.path("ownership.json").read_bytes() == before
        assert store.path("deployment.lease").read_bytes() == lease
    finally:
        subprocess.run(
            ["docker", "rm", "-f", identity], capture_output=True, check=False, timeout=10
        )
