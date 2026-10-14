"""Private Unix-socket bridges between hosted native nodes and the CI Docker VM.

SSH forwards only explicitly supplied control/data endpoints. It does not enable
GatewayPorts, discover Studio addresses, or forward an engine listener.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from integration.provision_upgrade_ci_vm import require_hosted_mac, studio_configuration

GUEST_ROOT = "/tmp/coire019-ci"


def scoped_ssh(root: Path, *, studio_workspaces: tuple[Path, Path] | None = None) -> list[str]:
    require_hosted_mac(root)
    metadata = root / "vm.json"
    if (
        root.resolve(strict=True) != root
        or root.stat().st_uid != os.getuid()
        or root.stat().st_mode & 0o077
        or metadata.is_symlink()
        or metadata.stat().st_uid != os.getuid()
        or metadata.stat().st_mode & 0o077
    ):
        raise RuntimeError("Native CI transport requires private owned VM authority")
    vm = json.loads(metadata.read_bytes())
    if studio_workspaces is not None:
        studio_configuration(
            studio_workspaces
        )  # Validate the actual protected roots, not metadata.
    instance = "coire019-studio-ci" if studio_workspaces is not None else "coire019-native-ci"
    config = root / "instances" / instance / "ssh.config"
    mounts = [str(path) for path in studio_workspaces] if studio_workspaces is not None else []
    role = "studio" if studio_workspaces is not None else "core"
    if (
        vm["execution"] != "github-hosted-native-ci"
        or vm.get("role", "core") != role
        or vm["host_mounts"] != mounts
        or vm["acceleration"] != "tcg"
        or vm["ssh_config"] != str(config)
        or config.is_symlink()
        or not config.resolve(strict=True).is_relative_to(root)
        or config.stat().st_uid != os.getuid()
        or config.stat().st_mode & 0o077
    ):
        raise RuntimeError("Native CI transport differs from isolated VM authority")
    return [
        "ssh",
        "-F",
        str(config),
        "-oControlMaster=no",
        "-oControlPath=none",
        "-oBatchMode=yes",
        "-oExitOnForwardFailure=yes",
        "-oServerAliveInterval=10",
        "-oServerAliveCountMax=3",
        "-l",
        "coire019-ci",
    ]


def forward_arguments(
    control_ports: tuple[int, int], data_ports: tuple[int, int], core_port: int
) -> list[str]:
    ports = (*control_ports, *data_ports, core_port)
    if any(isinstance(port, bool) or not 1024 <= port <= 65535 for port in ports):
        raise ValueError("CI forwards require explicit unprivileged endpoint ports")
    endpoints = [
        ("127.0.0.1", control_ports[0]),
        ("127.0.0.1", control_ports[1]),
        ("127.0.0.1", data_ports[0]),
        ("127.0.0.2", data_ports[1]),
        ("127.0.0.1", core_port),
    ]
    if len(set(endpoints)) != len(endpoints):
        raise ValueError("CI endpoint ports must be distinct")
    result = []
    for node, control, data in zip(("a", "b"), control_ports, data_ports, strict=True):
        for role, port in (("control", control), ("data", data)):
            host = "127.0.0.2" if node == "b" and role == "data" else "127.0.0.1"
            result.extend(["-R", f"{GUEST_ROOT}/bridges/node-{node}-{role}.sock:{host}:{port}"])
    result.extend(["-L", f"127.0.0.1:{core_port}:127.0.0.1:{core_port}"])
    return result


def start_forward(
    root: Path,
    control_ports: tuple[int, int],
    data_ports: tuple[int, int],
    core_port: int,
) -> subprocess.Popen[bytes]:
    command = scoped_ssh(root)
    subprocess.run(
        [
            *command,
            "lima-coire019-native-ci",
            shlex.join(["install", "-d", "-m", "0700", GUEST_ROOT + "/bridges"]),
        ],
        check=True,
        timeout=30,
        stdout=subprocess.DEVNULL,
    )
    output = root / "endpoint-forward.log"
    with output.open("xb") as log:
        output.chmod(0o600)
        return subprocess.Popen(
            [
                *command,
                *forward_arguments(control_ports, data_ports, core_port),
                "-N",
                "lima-coire019-native-ci",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
