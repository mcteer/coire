"""Transfer reviewed source and pinned development tools into the isolated CI VM.

No checkout credential configuration, host mounts, model weights or operator
secrets are transferred. The VM runs control-plane work only, as UID 65532.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integration.provision_upgrade_ci_vm import (
    require_control_plane_prototype,
    require_hosted_mac,
)

UV_VERSION = "0.12.7"
UV_SHA256 = "66393193038dd7eb108abd7a218d9cec04ac70ab98242b0720fa94de19223b7c"
GUEST_ROOT = "/tmp/coire019-ci"


def source_files(repo: Path) -> list[Path]:
    """Export source bytes without Git's credential-bearing local configuration."""
    listed = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo,
        timeout=30,
    )
    result = []
    for name in listed.decode().split("\0"):
        if not name:
            continue
        relative = Path(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.parts[0] in {"models", "data", "blobs", "dist"}
            or any(
                part in {".git", ".venv", "node_modules", "__pycache__"} for part in relative.parts
            )
            or any(part.startswith(".env") for part in relative.parts)
        ):
            continue
        path = repo / relative
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(repo):
            raise RuntimeError("CI source export contains a linked or foreign path")
        result.append(relative)
    return sorted(set(result))


def export_source(repo: Path, root: Path) -> tuple[Path, Path]:
    bundle = root / "source.bundle"
    subprocess.run(
        ["git", "bundle", "create", str(bundle), "HEAD"],
        cwd=repo,
        check=True,
        timeout=90,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    bundle.chmod(0o600)
    archive = root / "source.tar"
    with archive.open("xb") as output:
        archive.chmod(0o600)
        with tarfile.open(fileobj=output, mode="w") as contents:
            for relative in source_files(repo):
                contents.add(repo / relative, arcname=str(relative), recursive=False)
    return bundle, archive


def download_uv(root: Path) -> Path:
    path = root / "uv.tar.gz"
    url = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-aarch64-unknown-linux-gnu.tar.gz"
    with urllib.request.urlopen(url, timeout=60) as response, path.open("xb") as output:
        path.chmod(0o600)
        size = 0
        while chunk := response.read(1024**2):
            size += len(chunk)
            if size > 100 * 1024**2:
                raise RuntimeError("Pinned uv archive exceeds its provisioned size bound")
            output.write(chunk)
    if hashlib.sha256(path.read_bytes()).hexdigest() != UV_SHA256:
        raise RuntimeError("Pinned Linux uv archive integrity differs")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("vm_root", type=Path)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--control-plane-prototype", action="store_true")
    args = parser.parse_args()
    root: Path = args.vm_root
    if args.control_plane_prototype:
        require_control_plane_prototype(root)
    else:
        require_hosted_mac(root)
    if (
        root.resolve(strict=True) != root
        or root.stat().st_uid != os.getuid()
        or root.stat().st_mode & 0o077
    ):
        raise RuntimeError("CI VM tooling directory must be private, unlinked and owned")
    metadata = root / "vm.json"
    if (
        metadata.is_symlink()
        or metadata.stat().st_mode & 0o077
        or metadata.stat().st_uid != os.getuid()
    ):
        raise RuntimeError("CI VM metadata is not private and owned")
    vm = json.loads(metadata.read_bytes())
    expected = (
        "agent-control-plane-prototype"
        if args.control_plane_prototype
        else "github-hosted-native-ci"
    )
    config = root / "instances/coire019-native-ci/ssh.config"
    if (
        vm["execution"] != expected
        or vm["host_mounts"] != []
        or vm["acceleration"] != "tcg"
        or vm["ssh_config"] != str(config)
    ):
        raise RuntimeError("CI VM metadata differs from its isolated provisioning authority")
    ssh = ["ssh", "-F", str(config), "lima-coire019-native-ci"]
    # Use only Lima's freshly generated public key, never the operator's ~/.ssh.
    public = root / "instances/_config/user.pub"
    key = public.read_text().strip()
    if not key.startswith("ssh-") or "\n" in key:
        raise RuntimeError("CI VM SSH public key is not a single generated key")
    setup = """import json,os
from pathlib import Path
key=json.load(__import__('sys').stdin)['key']
root=Path('/home/coire019-ci/.ssh');root.mkdir(mode=0o700,exist_ok=True)
assert not root.is_symlink()
os.chown(root,65532,65532);root.chmod(0o700)
p=root/'authorized_keys';assert not p.is_symlink()
p.write_text(key+'\\n');os.chown(p,65532,65532);p.chmod(0o600)
"""
    subprocess.run(
        [*ssh, shlex.join(["sudo", "python3", "-c", setup])],
        input=json.dumps({"key": key}),
        text=True,
        check=True,
        timeout=30,
    )
    repo = args.repo.resolve(strict=True)
    transfer = root / ("source-transfer-" + uuid4().hex[:12])
    transfer.mkdir(mode=0o700)
    bundle, archive = export_source(repo, transfer)
    uv = download_uv(transfer)
    # Lima's existing ControlPath belongs to its bootstrap user. Reusing that
    # connection silently ignores the requested UID 65532 user.
    ci_ssh = [
        "ssh",
        "-F",
        str(config),
        "-oControlMaster=no",
        "-oControlPath=none",
        "-l",
        "coire019-ci",
        "lima-coire019-native-ci",
    ]
    identity = subprocess.check_output([*ci_ssh, "id -u"], text=True, timeout=30)
    if identity.strip() != "65532":
        raise RuntimeError("Isolated VM source transfer did not enter its scoped service UID")
    for source in (bundle, archive, uv):
        with source.open("rb") as contents:
            subprocess.run(
                [
                    *ci_ssh,
                    shlex.join(
                        [
                            "sh",
                            "-c",
                            "umask 077; cat > " + shlex.quote(GUEST_ROOT + "/" + source.name),
                        ]
                    ),
                ],
                stdin=contents,
                check=True,
                timeout=180,
            )
    install = (
        """from pathlib import Path
import hashlib,subprocess,tarfile
r=Path('/tmp/coire019-ci');assert r.stat().st_uid==65532
u=r/'uv.tar.gz';assert hashlib.sha256(u.read_bytes()).hexdigest()=="""
        + repr(UV_SHA256)
        + """
with tarfile.open(u) as t:t.extractall(r/'tools',filter='data')
subprocess.run(['cp',str(r/'tools/uv-aarch64-unknown-linux-gnu/uv'),str(r/'bin/uv')],check=True)
assert subprocess.check_output([str(r/'bin/uv'),'--version'],text=True).split()[:2]==['uv','0.12.7']
subprocess.run(['git','clone','--quiet',str(r/'source.bundle'),str(r/'repo')],check=True)
with tarfile.open(r/'source.tar') as t:t.extractall(r/'repo',filter='data')
"""
    )
    subprocess.run([*ci_ssh, shlex.join(["python3", "-c", install])], check=True, timeout=180)
    subprocess.run(
        [
            *ci_ssh,
            "cd /tmp/coire019-ci/repo && PATH=/tmp/coire019-ci/bin:$PATH uv sync --all-packages --frozen",
        ],
        check=True,
        timeout=600,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("Reviewed source and pinned uv installed in the isolated VM as UID 65532")


if __name__ == "__main__":
    main()
