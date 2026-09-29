"""Host-side preparation of complete, immutable credential generations."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

REQUIRED_SECRET_FILES = (
    "postgres_password",
    "key_signing_secret",
    "node_tokens",
    "admin_token",
    "bootstrap_admin_email",
)
OPTIONAL_SECRET_FILES = (
    "ops_service_token",
    "file_worker_service_token",
    "failover_peer_key",
    "failover_relay_token",
)
KEYCHAIN_ITEMS = {
    "postgres_password": "coire-postgres-password",
    "key_signing_secret": "coire-key-signing-secret",
    "node_tokens": "coire-node-tokens",
    "admin_token": "coire-admin-token",
    "bootstrap_admin_email": "coire-bootstrap-admin-email",
    "ops_service_token": "coire-ops-service-token",
    "file_worker_service_token": "coire-file-worker-service-token",
    "failover_peer_key": "coire-failover-peer-key",
}


class ReleaseInputError(ValueError):
    """A release input is incomplete or unsafe to use."""


def validate_project_name(value: str) -> str:
    """Constrain project names before using one as a state-directory component."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,62}", value):
        raise ReleaseInputError("invalid Compose project name")
    return value


def validate_external_secret_base(base: Path, source_root: Path) -> Path:
    """Reject credentials placed in the source tree, including via a parent symlink."""
    resolved = base.expanduser().resolve()
    if resolved.is_relative_to(source_root.resolve()):
        raise ReleaseInputError("credential base must be outside the repository")
    return resolved


def stage_secret_generation(root: Path, values: Mapping[str, str]) -> Path:
    """Validate all inputs before writing an unpublished generation.

    Callers select a new generation for container recreation only after it is complete.
    Existing mounted files are never overwritten in place.
    """
    missing = [name for name in REQUIRED_SECRET_FILES if not values.get(name)]
    if missing:
        raise ReleaseInputError(f"missing required credential: {', '.join(missing)}")
    if root.is_symlink():
        raise ReleaseInputError("credential directory must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    generation = Path(tempfile.mkdtemp(prefix="generation-", dir=root))
    generation.chmod(0o700)
    try:
        for name in (*REQUIRED_SECRET_FILES, *OPTIONAL_SECRET_FILES):
            value = values.get(name, "")
            target = generation / name
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "wb") as out:
                out.write(value.encode("utf-8"))
                out.flush()
                os.fsync(out.fileno())
            # Docker file-secret bind mounts retain the host uid; uid 65532 must read
            # the file after its 0700 parent has been traversed by the daemon.
            target.chmod(0o644)
        return generation
    except BaseException:
        shutil.rmtree(generation)
        raise


def release_manifest(
    resolved: dict[str, Any],
    *,
    secret_dir: Path,
    cluster_dir: Path,
    image_ids: Mapping[str, str],
) -> dict[str, Any]:
    """Freeze bind sources and image IDs after Compose resolves all interpolation."""
    manifest = cast(dict[str, Any], json.loads(json.dumps(resolved)))
    for name, source in manifest.get("secrets", {}).items():
        if name not in (*REQUIRED_SECRET_FILES, *OPTIONAL_SECRET_FILES):
            raise ReleaseInputError(f"unexpected Compose secret: {name}")
        source["file"] = str(secret_dir / name)
    for name, service in manifest["services"].items():
        service.pop("build", None)
        image = service.get("image", "")
        if image.startswith("coire-") or "/coire-" in image:
            if name not in image_ids:
                raise ReleaseInputError(f"missing image ID for {name}")
            service["image"] = image_ids[name]
        for volume in service.get("volumes", []):
            if volume.get("target") == "/run/coire/cluster":
                volume["source"] = str(cluster_dir)
                volume["bind"] = {"create_host_path": False}
    return manifest


def matching_active_generation(
    state_root: Path, base: Path, project: str, values: Mapping[str, str]
) -> Path | None:
    """Reuse the current immutable files when Keychain material has not changed."""
    manifest_path = state_root / project / "current" / "compose.json"
    try:
        manifest = json.loads(manifest_path.read_text())
        files = manifest["secrets"]
        paths = {name: Path(files[name]["file"]) for name in REQUIRED_SECRET_FILES}
        parents = {path.parent for path in paths.values()}
        if len(parents) != 1:
            return None
        generation = parents.pop()
        if (
            generation.is_symlink()
            or generation.resolve().parent != (base / project / "secrets").resolve()
        ):
            return None
        paths.update({name: generation / name for name in OPTIONAL_SECRET_FILES})
        for name, path in paths.items():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.read_bytes() != values.get(name, "").encode()
            ):
                return None
        return generation
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _read_keychain(name: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", name],
        capture_output=True,
        check=False,
        text=True,
    )
    return result.stdout.rstrip("\n") if result.returncode == 0 else ""


def read_secret_inputs(*, from_env: bool) -> dict[str, str]:
    """Read every configured source into memory before creating any credential file."""
    values: dict[str, str] = {}
    for filename, item in KEYCHAIN_ITEMS.items():
        env_name = "COIRE_SECRET_" + filename.upper()
        values[filename] = os.environ.get(env_name, "") if from_env else _read_keychain(item)
        if from_env and filename in REQUIRED_SECRET_FILES and not values[filename]:
            raise ReleaseInputError(f"missing environment secret: {env_name}")
    if (
        "ops" in os.environ.get("COMPOSE_PROFILES", "").split(",")
        and not values["ops_service_token"]
    ):
        raise ReleaseInputError("ops profile requires coire-ops-service-token")
    if (
        "chat-files" in os.environ.get("COMPOSE_PROFILES", "").split(",")
        and not values["file_worker_service_token"]
    ):
        raise ReleaseInputError("chat-files profile requires coire-file-worker-service-token")
    if os.environ.get("COIRE_FAILOVER_MEMBER_NAME") == "coire-core":
        for key in (
            "COIRE_FAILOVER_CORE_PUBLIC_KEY",
            "COIRE_FAILOVER_EDGE_A_PUBLIC_KEY",
            "COIRE_FAILOVER_EDGE_B_PUBLIC_KEY",
        ):
            if not os.environ.get(key):
                raise ReleaseInputError(f"missing failover membership key: {key}")
        if not values["failover_peer_key"]:
            raise ReleaseInputError("missing core failover signing key")
    else:
        values["failover_peer_key"] = ""
    values["failover_relay_token"] = ""
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare isolated Coire deployment credentials")
    parser.add_argument("prepare", nargs="?", choices=["prepare"])
    parser.add_argument("--secrets-from-env", action="store_true")
    args = parser.parse_args()
    try:
        project = validate_project_name(os.environ.get("COMPOSE_PROJECT_NAME", "coire"))
        base = Path(os.environ.get("COIRE_SECRETS_BASE", str(Path.home() / ".coire" / "projects")))
        source_root = os.environ.get("COIRE_REPO_ROOT")
        if source_root:
            base = validate_external_secret_base(base, Path(source_root))
        if base.is_symlink():
            raise ReleaseInputError("credential base must not be a symlink")
        values = read_secret_inputs(from_env=args.secrets_from_env)
        state_root = Path(
            os.environ.get("COIRE_STATE_ROOT", str(Path.home() / ".coire" / "projects"))
        )
        generation = matching_active_generation(state_root, base, project, values)
        if generation is None:
            generation = stage_secret_generation(base / project / "secrets", values)
    except ReleaseInputError as exc:
        parser.exit(2, f"{exc}\n")
    print(generation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
