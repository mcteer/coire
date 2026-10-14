"""Real installed native Source/guard startup on a disposable hosted Mac.

This supplements accepted-operation Studio acceptance; it checks installed
runtime production and independent ownership without consulting an operator.
"""

import os
import secrets
import socket
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from integration.provision_upgrade_ci_vm import require_hosted_mac
from integration.upgrade_native_ci_wheels import (
    initialize_source_bootstrap,
    install_initial_source,
    prepare_release,
)

from coire_core.models.upgrades import NativeGuardStatus, NativeNodeProcess
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore

pytestmark = [pytest.mark.integration, pytest.mark.native_upgrade]


def test_installed_source_and_independent_guard_survive_node_sigkill() -> None:
    root = Path(os.environ["RUNNER_TEMP"]) / "coire-019-acceptance/native-upgrade-lab-ci"
    require_hosted_mac(root)
    assert not root.exists()
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    repo = Path(__file__).resolve().parents[2]
    wheels = repo / "dist"
    install_initial_source(repo, root, wheels)
    source, request = prepare_release(
        root,
        wheels,
        environment_id=(root / "envs/current").resolve().name,
        owner=uuid4(),
        revision=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
        compatible_sources=("f" * 64,),
        label="source",
    )
    initialize_source_bootstrap(repo, root, "coire-edge-a", request)
    token = secrets.token_urlsafe(32)
    ports = []
    for _ in range(2):
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            ports.append(available.getsockname()[1])
    assert len(set(ports)) == 2
    environment = dict(os.environ)
    for name, field in Settings.model_fields.items():
        if isinstance(field.default, str) and field.default.startswith("/opt/coire/"):
            environment[name.upper()] = str(root / field.default.removeprefix("/opt/coire/"))
    environment.update(
        NODE_NAME="coire-edge-a",
        NODE_REGISTRATION_ENABLED="false",
        UPGRADES_ENABLED="true",
        LEGACY_NETWORK_MODE="false",
        NODE_CONTROL_HOST="127.0.0.1",
        NODE_DATA_HOST="127.0.0.1",
        NODE_LISTEN_PORT=str(ports[0]),
        NODE_DATA_LISTEN_PORT=str(ports[1]),
        NODE_TOKEN=token,
        NODE_REGISTRATION_TOKEN=secrets.token_urlsafe(32),
        HF_TOKEN="isolated-offline-fixture",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        FAILOVER_PEER_KEY=secrets.token_urlsafe(32),
        FAILOVER_CORE_PUBLIC_KEY="isolated-unused",
        FAILOVER_RELAY_TOKEN=secrets.token_urlsafe(32),
        DIAGNOSTICS_ENABLED="false",
        OTLP_ENDPOINT="http://127.0.0.1:1",
    )
    python = root / "envs" / str(source.manifest.environment_id) / "bin/python3"
    guard_store = PrivateUpgradeStore(root / "upgrade-bootstrap", create=False)
    state = PrivateUpgradeStore(root / "state/upgrades")
    processes: list[subprocess.Popen[bytes]] = []
    with (root / "log/hosted-runtime.log").open("xb") as output:
        (root / "log/hosted-runtime.log").chmod(0o600)

        def start(module: str) -> subprocess.Popen[bytes]:
            child = subprocess.Popen(
                [str(python), "-I", "-B", "-m", module],
                env=environment,
                stdout=output,
                stderr=output,
                stdin=subprocess.DEVNULL,
            )
            processes.append(child)
            return child

        try:
            guard = start("coire_node.upgrade_guard")
            deadline = time.monotonic() + 60
            status = None
            while time.monotonic() < deadline:
                status = guard_store.read("status.json", NativeGuardStatus)
                if status is not None and status.ready:
                    break
                assert guard.poll() is None
                time.sleep(0.2)
            assert status is not None and status.ready
            guard_identity = status.process
            old_pid = None
            with httpx.Client(
                base_url=f"http://127.0.0.1:{ports[0]}",
                headers={"Authorization": "Bearer " + token},
                timeout=2,
                trust_env=False,
            ) as client:
                for attempt in range(2):
                    node = start("coire_node")
                    deadline = time.monotonic() + 60
                    ready = False
                    while time.monotonic() < deadline:
                        assert node.poll() is None
                        with suppress(httpx.TransportError):
                            ready = client.get("/node/health").status_code == 200
                        if ready:
                            break
                        time.sleep(0.2)
                    assert ready
                    observed = state.read("node-process.json", NativeNodeProcess)
                    assert observed is not None and observed.process.pid == node.pid
                    assert observed.selected.release_sha256 == source.manifest_sha256
                    assert observed.process.pid != old_pid
                    status = guard_store.read("status.json", NativeGuardStatus)
                    assert status is not None and status.process == guard_identity
                    assert (root / "envs/current").resolve().name == source.manifest.environment_id
                    if attempt == 0:
                        old_pid = node.pid
                        node.kill()
                        node.wait(timeout=10)
        finally:
            for child in reversed(processes):
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=15)
