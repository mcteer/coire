#!/usr/bin/env python3
"""Select and hash-check locked CPython 3.13/macOS arm64 node wheels."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

from packaging.markers import Marker
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename


def locked_wheels(pylock: Path) -> list[tuple[str, str, int]]:
    """Return one compatible immutable URL/hash/size per active dependency."""
    data = tomllib.loads(pylock.read_text())
    supported = {tag: rank for rank, tag in enumerate(sys_tags())}
    chosen: list[tuple[str, str, int]] = []
    for package in data["packages"]:
        marker = package.get("marker")
        if marker and not Marker(marker).evaluate():
            continue
        candidates: list[tuple[int, dict[str, object]]] = []
        for wheel in package.get("wheels", []):
            url = wheel["url"]
            filename = Path(urllib.parse.urlparse(url).path).name
            _, _, _, tags = parse_wheel_filename(filename)
            ranks = [supported[tag] for tag in tags if tag in supported]
            if ranks:
                candidates.append((min(ranks), wheel))
        if not candidates:
            raise ValueError(f"no locked macOS arm64 wheel for {package['name']}")
        _, wheel = min(candidates, key=lambda item: item[0])
        url = str(wheel["url"])
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "files.pythonhosted.org":
            raise ValueError(f"unexpected wheel host for {package['name']}")
        hashes = wheel.get("hashes")
        size = wheel.get("size")
        if (
            not isinstance(hashes, dict)
            or not isinstance(hashes.get("sha256"), str)
            or not isinstance(size, int)
        ):
            raise ValueError(f"missing locked wheel integrity for {package['name']}")
        chosen.append((url, hashes["sha256"], size))
    return chosen


def stage(wheels: list[tuple[str, str, int]], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for url, expected_hash, expected_size in wheels:
        name = Path(urllib.parse.urlparse(url).path).name
        target = destination / name
        if target.exists():
            existing_digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if target.stat().st_size == expected_size and existing_digest == expected_hash:
                continue
            raise ValueError(f"staged wheel hash mismatch: {name}")
        temporary = destination / f".{name}.{os.getpid()}.part"
        hasher = hashlib.sha256()
        size = 0
        try:
            with (
                urllib.request.urlopen(url, timeout=60) as response,
                temporary.open("xb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > expected_size:
                        raise ValueError(f"wheel exceeds locked size: {name}")
                    hasher.update(chunk)
                    output.write(chunk)
            if size != expected_size or hasher.hexdigest() != expected_hash:
                raise ValueError(f"wheel hash mismatch: {name}")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pylock", type=Path)
    parser.add_argument("wheel_dir", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if (
        sys.version_info[:2] != (3, 13)
        or platform.system() != "Darwin"
        or platform.machine() != "arm64"
    ):
        raise SystemExit("node wheels must be selected on CPython 3.13/macOS arm64")
    wheels = locked_wheels(args.pylock)
    if not args.check_only:
        stage(wheels, args.wheel_dir)
    print(f"selected {len(wheels)} locked macOS arm64 node wheels")


if __name__ == "__main__":
    main()
