"""Metadata-only validation for the opt-in offline training test asset."""

from __future__ import annotations

import json
import platform
from pathlib import Path

from coire_core.models.jobs import ChecksumManifest
from coire_node.store import sha256_file

MAX_TEST_MODEL_BYTES = 1024**3


def offline_training_model(value: str | None) -> Path:
    """Refuse core before any weight load; verify a local acquired fixture, never fetch."""
    host = platform.node().lower().split(".", 1)[0]
    if host == "coire-core":
        raise ValueError("training engine tests cannot execute on core (Constitution II)")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("training engine tests require an Apple Silicon Mac")
    if not value:
        raise ValueError("COIRE_TEST_MODEL must name an acquired local model of at most 1 GiB")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("COIRE_TEST_MODEL must be an absolute local directory without symlinks")
    path = path.resolve()
    manifest_path = path.with_name(path.name + ".manifest.json")
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("training fixture requires its adjacent acquisition checksum manifest")
    manifest = ChecksumManifest.model_validate_json(manifest_path.read_bytes())
    if (
        manifest.slug != path.name
        or not manifest.files
        or manifest.total_bytes > MAX_TEST_MODEL_BYTES
    ):
        raise ValueError("training fixture identity/size does not meet the tiny-model gate")
    listed = {entry.path for entry in manifest.files}
    if len(listed) != len(manifest.files) or "config.json" not in listed:
        raise ValueError("training fixture requires unique manifest files including config")
    actual: set[str] = set()
    for file in path.rglob("*"):
        relative = file.relative_to(path)
        if relative.parts[0] == ".cache":
            continue
        if file.is_symlink():
            raise ValueError("training fixture must not contain linked files or directories")
        if file.is_file():
            actual.add(relative.as_posix())
    if actual != listed:
        raise ValueError("training fixture contains unmanifested or missing files")
    total = 0
    for entry in manifest.files:
        file = path / entry.path
        if file.is_symlink() or not file.resolve().is_relative_to(path) or not file.is_file():
            raise ValueError("training fixture manifest contains a missing or linked file")
        size = file.stat().st_size
        total += size
        if total > MAX_TEST_MODEL_BYTES or size != entry.bytes or sha256_file(file) != entry.sha256:
            raise ValueError("training fixture failed checksum/size verification")
    if total != manifest.total_bytes:
        raise ValueError("training fixture manifest total differs from verified files")
    config = json.loads((path / "config.json").read_bytes())
    if not isinstance(config, dict) or config.get("model_file") or config.get("auto_map"):
        raise ValueError("training fixture must not require executable model configuration")
    if not any(entry.path.endswith(".safetensors") for entry in manifest.files):
        raise ValueError("training fixture has no safetensors weights")
    return path
