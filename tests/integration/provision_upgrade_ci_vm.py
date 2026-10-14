"""Provision a private software-emulated Linux Docker host for native Mac CI.

This host runs control-plane containers only. Native models remain on the
ephemeral Apple Silicon runner. It has no host mounts or dependency on nested
virtualization, Docker Desktop, OrbStack, or operator Studio SSH access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

import yaml

LIMA_VERSION = "2.1.4"
LIMA_SHA256 = "14c5b283f1c5eb4078e5a300b8d241f69197a3e41326dfc685a69c9455917acf"
QEMU_VERSION = "11.1.2"
UBUNTU_SHA256 = "cafa1a965b591b7c4184b484ffd8e625981a79d48f9b4ae8a4adf7b4c5ade927"


def installed_native_paths() -> tuple[Path, ...]:
    return (
        Path("/opt/coire/envs/current"),
        Path("/Library/LaunchDaemons/com.coire.node.plist"),
        Path("/Library/LaunchDaemons/com.coire.upgrade-guard.plist"),
        Path.home() / "Library/LaunchAgents/com.coire.node.plist",
        Path.home() / "Library/LaunchAgents/com.coire.upgrade-guard.plist",
    )


def require_hosted_mac(root: Path) -> None:
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or os.environ.get("RUNNER_OS") != "macOS"
        or os.environ.get("RUNNER_ARCH") != "ARM64"
        or platform.system() != "Darwin"
        or platform.machine() != "arm64"
    ):
        raise RuntimeError("Native CI provisioning requires a hosted Apple Silicon runner")
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    if not root.is_absolute() or not root.resolve().is_relative_to(temporary):
        raise RuntimeError("Native CI tooling must remain under its ephemeral runner directory")
    if os.getuid() == 0 or any(
        path.exists() or path.is_symlink() for path in installed_native_paths()
    ):
        raise RuntimeError("Native CI refuses root or an installed operator node")


def require_control_plane_prototype(root: Path) -> None:
    """Allow agent-run Linux-only validation without claiming hosted native CI."""
    if (
        os.environ.get("GITHUB_ACTIONS") == "true"
        or platform.system() != "Darwin"
        or platform.machine() != "arm64"
        or not root.is_absolute()
        or not root.resolve().is_relative_to(Path("/private/tmp").resolve(strict=True))
        or not root.name.startswith("coire-019-native-ci-prototype-")
    ):
        raise RuntimeError("Control-plane prototype cannot bypass hosted CI isolation")


def download_lima(root: Path) -> Path:
    archive = root / "lima.tar.gz"
    if not archive.exists():
        url = (
            "https://github.com/lima-vm/lima/releases/download/v"
            + LIMA_VERSION
            + "/lima-"
            + LIMA_VERSION
            + "-Darwin-arm64.tar.gz"
        )
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("xb") as output:
            archive.chmod(0o600)
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 100 * 1024**2:
                    raise RuntimeError("Pinned Lima archive exceeds its provisioned size bound")
                output.write(chunk)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != LIMA_SHA256:
        raise RuntimeError("Pinned Lima archive integrity differs")
    destination = root / "lima"
    destination.mkdir(mode=0o700, exist_ok=True)
    with tarfile.open(archive) as compressed:
        compressed.extractall(destination, filter="data")
    binary = destination / "bin/limactl"
    version = subprocess.check_output([str(binary), "--version"], text=True, timeout=10)
    if version.strip() != "limactl version " + LIMA_VERSION:
        raise RuntimeError("Installed Lima version differs from its pinned archive")
    return binary


def configuration() -> str:
    return f"""vmType: qemu
arch: aarch64
cpuType:
  aarch64: max
cpus: 2
memory: 3GiB
disk: 20GiB
images:
- location: https://cloud-images.ubuntu.com/releases/noble/release-20260615/ubuntu-24.04-server-cloudimg-arm64.img
  arch: aarch64
  digest: sha256:{UBUNTU_SHA256}
mounts: []
containerd:
  system: false
  user: false
ssh:
  loadDotSSHPubKeys: false
hostResolver:
  hosts:
    host.docker.internal: host.lima.internal
portForwards:
- guestSocket: /run/docker.sock
  hostSocket: '{{{{.Dir}}}}/sock/docker.sock'
- guestPortRange: [1, 65535]
  guestIP: 0.0.0.0
  guestIPMustBeZero: false
  proto: any
  ignore: true
provision:
- mode: system
  script: |
    #!/bin/sh
    set -eu
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y \\
      docker.io=29.1.3-0ubuntu3~24.04.2 \\
      docker-compose-v2=2.40.3+ds1-0ubuntu1~24.04.1 \\
      containerd=2.2.1-0ubuntu1~24.04.3 \\
      runc=1.3.4-0ubuntu1~24.04.1 \\
      git rsync ca-certificates
    systemctl enable --now docker
    useradd --uid 65532 --user-group --create-home --groups docker coire019-ci
    install -d -o 65532 -g 65532 -m 0700 /tmp/coire019-ci /tmp/coire019-ci/bin
probes:
- script: |
    #!/bin/sh
    set -eu
    coire_ci_docker_version="$(sudo docker version --format json | python3 -c 'import json,sys;print(json.load(sys.stdin)["Server"]["Version"])')"
    coire_ci_compose_version="$(sudo docker compose version --short)"
    test "$coire_ci_docker_version" = 29.1.3
    test "$coire_ci_compose_version" = 2.40.3+ds1-0ubuntu1~24.04.1
    test "$(id -u coire019-ci)" = 65532
"""


def studio_configuration(workspaces: tuple[Path, Path]) -> str:
    """A separate Studio-role Docker VM sees only the two harness workspaces."""
    if len(set(workspaces)) != 2:
        raise RuntimeError("Studio CI requires two distinct workspace roots")
    for workspace in workspaces:
        require_hosted_mac(workspace)
        if (
            workspace.name != "workspaces"
            or not workspace.parent.name.startswith("native-upgrade-lab-")
            or workspace.parent.parent.name != "coire-019-acceptance"
            or workspace.resolve(strict=True) != workspace
            or workspace.stat().st_uid != os.getuid()
            or workspace.stat().st_mode & 0o077
        ):
            raise RuntimeError("Studio CI mount must be an owned private harness workspace")
    body = yaml.safe_load(configuration())
    body["cpus"] = 1
    body["memory"] = "768MiB"
    body["disk"] = "8GiB"
    body["mountType"] = "9p"
    body["mounts"] = [
        {"location": str(workspace), "mountPoint": str(workspace), "writable": True}
        for workspace in workspaces
    ]
    return yaml.safe_dump(body, sort_keys=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--control-plane-prototype", action="store_true")
    parser.add_argument("--studio-workspaces", type=Path, nargs=2)
    args = parser.parse_args()
    root: Path = args.root
    workspaces = tuple(args.studio_workspaces) if args.studio_workspaces else None
    if args.control_plane_prototype:
        if workspaces is not None:
            raise RuntimeError("The control-plane prototype cannot run user harnesses")
        require_control_plane_prototype(root)
    else:
        require_hosted_mac(root)
    if root.exists():
        raise RuntimeError("Native CI VM tooling requires a fresh owned directory")
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    qemu = shutil.which("qemu-system-aarch64")
    if qemu is None:
        raise RuntimeError("Pinned QEMU must be installed before VM provisioning")
    version = subprocess.check_output([qemu, "--version"], text=True, timeout=10)
    if version.splitlines()[0] != "QEMU emulator version " + QEMU_VERSION:
        raise RuntimeError("QEMU differs from the tested software-emulation version")
    lima = download_lima(root)
    instance_root = root / "instances"
    instance_root.mkdir(mode=0o700)
    instance = "coire019-studio-ci" if workspaces is not None else "coire019-native-ci"
    source = root / (instance + ".yaml")
    if workspaces is not None:
        if len(workspaces) != 2:
            raise RuntimeError("Studio CI requires exactly two harness workspace roots")
        source.write_text(studio_configuration((workspaces[0], workspaces[1])))
    else:
        source.write_text(configuration())
    source.chmod(0o600)
    environment = {
        **os.environ,
        "LIMA_HOME": str(instance_root),
        # Lima otherwise selects HVF on a physical development Mac. Explicit
        # TCG keeps the tested path identical to a hosted VM without nested HVF.
        "QEMU_SYSTEM_AARCH64": shlex.join([qemu, "-machine", "virt,accel=tcg"]),
    }
    log = root / "provision.log"
    with log.open("xb") as output:
        log.chmod(0o600)
        result = subprocess.run(
            [str(lima), "start", "--tty=false", str(source)],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=1800,
        )
    if result.returncode:
        raise RuntimeError("Private native CI VM provisioning failed; retain provision.log")
    details = {
        "lima": str(lima),
        "lima_home": str(instance_root),
        "instance": instance,
        "role": "studio" if workspaces is not None else "core",
        "ssh_config": str(instance_root / instance / "ssh.config"),
        "docker_socket": str(instance_root / instance / "sock/docker.sock"),
        "acceleration": "tcg",
        "host_mounts": [str(workspace) for workspace in workspaces] if workspaces else [],
        "execution": "agent-control-plane-prototype"
        if args.control_plane_prototype
        else "github-hosted-native-ci",
    }
    record = root / "vm.json"
    record.write_text(json.dumps(details, sort_keys=True))
    record.chmod(0o600)
    print("Private software-emulated control-plane VM provisioned")


if __name__ == "__main__":
    main()
