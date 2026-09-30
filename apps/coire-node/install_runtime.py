"""Smoke a staged node environment before atomically making it active."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


def smoke(python: Path) -> None:
    subprocess.run(
        [str(python), "-c", "import coire_core, coire_node, mlx_lm, mlx_vlm"],
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
    if len(sys.argv) != 4:
        raise SystemExit("usage: install_runtime.py SOURCE TARGET CURRENT")
    publish_environment(*(Path(value) for value in sys.argv[1:]))
