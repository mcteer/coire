"""Smoke a staged node environment before atomically making it active."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path


def build_uuid(executable: Path) -> tuple[int, bytes]:
    """Read the UUID location from the supported thin arm64 Mach-O format."""
    with executable.open("rb") as binary:
        header = binary.read(32)
        if len(header) != 32:
            raise ValueError("truncated Mach-O header")
        magic, cpu, _subtype, _kind, commands, command_bytes, _flags, _reserved = struct.unpack(
            "<8I", header
        )
        if magic != 0xFEEDFACF or cpu != 0x0100000C:
            raise ValueError("Coire runtime requires a thin arm64 Mach-O executable")
        end = 32 + command_bytes
        if end > executable.stat().st_size:
            raise ValueError("truncated Mach-O load commands")
        offset = 32
        for _ in range(commands):
            binary.seek(offset)
            command_header = binary.read(8)
            if len(command_header) != 8 or offset + 8 > end:
                raise ValueError("truncated Mach-O load command")
            command, size = struct.unpack("<2I", command_header)
            if size < 8 or offset + size > end:
                raise ValueError("invalid Mach-O load command size")
            if command == 0x1B:  # LC_UUID
                if size != 24:
                    raise ValueError("invalid Mach-O build UUID")
                return offset + 8, binary.read(16)
            offset += size
        raise ValueError("Mach-O build UUID missing")


def runtime_uuid(original: bytes) -> bytes:
    return uuid.uuid5(uuid.NAMESPACE_URL, "com.coire.node.runtime:" + original.hex()).bytes


def isolate_build_uuid(executable: Path) -> None:
    """Separate the copied arm64 Mach-O identity before signing (Apple TN3178)."""
    offset, original = build_uuid(executable)
    with executable.open("r+b") as binary:
        binary.seek(offset)
        binary.write(runtime_uuid(original))


def network_python(source: Path) -> Path:
    """Give Coire a distinct executable without changing uv's shared interpreter."""
    source = source.resolve(strict=True)
    target = source.with_name("coire-node-python")
    temporary = target.with_name(f".coire-node-python.{os.getpid()}")
    identity = "com.coire.node.runtime"
    try:
        if not target.exists():
            shutil.copy2(source, temporary)
            isolate_build_uuid(temporary)
            subprocess.run(
                ["codesign", "--force", "--sign", "-", "--identifier", identity, str(temporary)],
                check=True,
                timeout=30,
            )
            candidate = temporary
        else:
            candidate = target
        subprocess.run(["codesign", "--verify", str(candidate)], check=True, timeout=30)
        signature = subprocess.run(
            ["codesign", "--display", "--verbose=4", str(candidate)],
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
        )
        if f"Identifier={identity}" not in signature.stderr.splitlines():
            raise ValueError("unexpected Coire runtime signing identity")
        if build_uuid(candidate)[1] != runtime_uuid(build_uuid(source)[1]):
            raise ValueError("unexpected Coire runtime build UUID")
        if candidate == temporary:
            os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def smoke(python: Path) -> None:
    subprocess.run(
        [str(python), "-c", "import coire_core, coire_node, mlx_lm, mlx_vlm, mflux"],
        check=True,
        timeout=30,
        stdout=subprocess.DEVNULL,
    )
    for module in ("mlx_lm.server", "mlx_vlm.server"):
        subprocess.run(
            [str(python), "-m", module, "--help"],
            check=True,
            timeout=30,
            stdout=subprocess.DEVNULL,
        )
    # Import the pinned native entry point without loading weights or contacting the Hub.
    subprocess.run(
        [str(python), "-c", "from mflux.models.z_image.variants.z_image import ZImage"],
        check=True,
        timeout=30,
        stdout=subprocess.DEVNULL,
    )


def publish_environment(
    source: Path,
    target: Path,
    current: Path,
    *,
    verify: Callable[[Path], None] = smoke,
) -> None:
    """Keep the prior link on any smoke failure; never mutate an active environment."""
    if not source.is_dir() or not (source / "bin/python3").is_file():
        raise ValueError("node environment is incomplete")
    verify(source / "bin/python3")
    if source != target:
        if target.exists():
            raise FileExistsError("immutable node environment already exists")
        os.replace(source, target)
    temporary = current.with_name(f".current.{os.getpid()}")
    try:
        os.symlink(target, temporary)
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--network-python":
        print(network_python(Path(sys.argv[2])))
        raise SystemExit(0)
    if len(sys.argv) != 4:
        raise SystemExit("usage: install_runtime.py SOURCE TARGET CURRENT")
    publish_environment(*(Path(value) for value in sys.argv[1:]))
