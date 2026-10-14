"""Measure native CI disk capacity without changing the training admission floor.

Only an ephemeral hosted runner may remove unused Xcode installations. The
selected developer tools and every unrelated application remain intact.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integration.provision_upgrade_ci_vm import require_hosted_mac


def unused_xcodes(applications: Path, selected: Path) -> list[Path]:
    selected = selected.resolve(strict=True)
    result = []
    for candidate in sorted(applications.glob("Xcode*.app")):
        if candidate.is_symlink() or not candidate.is_dir():
            raise RuntimeError("Hosted Xcode candidate is linked or is not a directory")
        candidate = candidate.resolve(strict=True)
        if selected.is_relative_to(candidate):
            continue
        metadata = candidate / "Contents/Info.plist"
        if metadata.is_symlink() or not metadata.is_file():
            raise RuntimeError("Hosted Xcode identity is missing or linked")
        with metadata.open("rb") as source:
            body = plistlib.load(source)
        if body.get("CFBundleIdentifier") != "com.apple.dt.Xcode":
            raise RuntimeError("Hosted Xcode candidate has a different application identity")
        result.append(candidate)
    return result


def prepare_capacity(root: Path, minimum_free_bytes: int, *, remove_unused: bool) -> None:
    require_hosted_mac(root)
    if minimum_free_bytes < 20 * 1024**3:
        raise ValueError("Native CI cannot lower the actual training disk floor")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (
        root.resolve(strict=True) != root
        or root.stat().st_uid != os.getuid()
        or root.stat().st_mode & 0o077
    ):
        raise RuntimeError("Hosted capacity record root must be private, unlinked and owned")
    selected = Path(
        subprocess.check_output(["xcode-select", "--print-path"], text=True, timeout=10).strip()
    )
    selected.resolve(strict=True)
    before = shutil.disk_usage(root).free
    removed = []
    if remove_unused and before < minimum_free_bytes:
        for candidate in unused_xcodes(Path("/Applications"), selected):
            subprocess.run(["sudo", "rm", "-rf", "--", str(candidate)], check=True, timeout=300)
            removed.append(str(candidate))
            if shutil.disk_usage(root).free >= minimum_free_bytes:
                break
    after = shutil.disk_usage(root).free
    record = root / "capacity.json"
    with record.open("x") as destination:
        record.chmod(0o600)
        json.dump(
            {
                "before_free_bytes": before,
                "after_free_bytes": after,
                "required_free_bytes": minimum_free_bytes,
                "selected_developer_tools": str(selected),
                "removed_unused_xcodes": removed,
            },
            destination,
        )
    if after < minimum_free_bytes:
        raise RuntimeError("Hosted runner lacks measured disk capacity; retain capacity.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--minimum-free-gib", type=int, default=32)
    parser.add_argument("--remove-unused-xcodes", action="store_true")
    arguments = parser.parse_args()
    prepare_capacity(
        arguments.root,
        arguments.minimum_free_gib * 1024**3,
        remove_unused=arguments.remove_unused_xcodes,
    )
