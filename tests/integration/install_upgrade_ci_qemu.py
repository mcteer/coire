"""Install the tested QEMU formula on an ephemeral hosted Mac only."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integration.provision_upgrade_ci_vm import QEMU_VERSION, require_hosted_mac

FORMULA_REVISION = "640c5a2f2c9669914729543ca869e93fbcbae02d"
FORMULA_SHA256 = "b8fa843b66a8879edf2f4c2440a4990557762764fc7f46767a26929d355215c7"
TAP = "coire/upgrade-ci"


def verify_formula(content: bytes) -> None:
    if hashlib.sha256(content).hexdigest() != FORMULA_SHA256:
        raise RuntimeError("Pinned QEMU formula integrity differs")


def install(root: Path) -> None:
    require_hosted_mac(root)
    if root.exists():
        raise RuntimeError("QEMU tooling requires a fresh owned directory")
    root.mkdir(mode=0o700)
    brew = shutil.which("brew")
    if brew is None:
        raise RuntimeError("Hosted Mac has no Homebrew installer")
    url = (
        "https://raw.githubusercontent.com/Homebrew/homebrew-core/"
        + FORMULA_REVISION
        + "/Formula/q/qemu.rb"
    )
    with urllib.request.urlopen(url, timeout=60) as response:
        content = response.read(1024 * 1024 + 1)
    verify_formula(content)
    subprocess.run([brew, "tap-new", TAP], check=True, timeout=60)
    tap_root = Path(
        subprocess.check_output([brew, "--repository", TAP], text=True, timeout=30).strip()
    )
    formula = tap_root / "Formula/qemu.rb"
    if formula.exists() or formula.is_symlink():
        raise RuntimeError("Ephemeral QEMU tap already contains a formula")
    formula.write_bytes(content)
    log = root / "install.log"
    with log.open("xb") as output:
        log.chmod(0o600)
        subprocess.run(
            [brew, "install", "--force-bottle", TAP + "/qemu"],
            check=True,
            timeout=1200,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    binary = shutil.which("qemu-system-aarch64")
    if binary is None:
        raise RuntimeError("Pinned QEMU was not installed")
    version = subprocess.check_output([binary, "--version"], text=True, timeout=10)
    if version.splitlines()[0] != "QEMU emulator version " + QEMU_VERSION:
        raise RuntimeError("Installed QEMU differs from the tested version")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    install(parser.parse_args().root)
