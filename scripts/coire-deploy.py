#!/usr/bin/env python3
"""Install a source-independent Compose release and validate existing database access."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from coire_core.deploy import ReleaseInputError, release_manifest, validate_project_name

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose" / "compose.yaml"
PROBE = (
    "import asyncio,asyncpg\n"
    "from coire_core.settings import Settings\n"
    "s=Settings()\n"
    "async def probe():\n"
    " c=await asyncpg.connect(host=s.postgres_host,port=s.postgres_port,"
    "user=s.postgres_user,password=s.postgres_password.get_secret_value(),"
    "database=s.postgres_db,timeout=3)\n"
    " await c.fetchval('SELECT 1')\n"
    " await c.close()\n"
    "asyncio.run(probe())"
)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, cwd=COMPOSE.parent
    )


def source_files() -> list[str]:
    configured = os.environ.get("COMPOSE_FILE")
    if not configured:
        return [str(COMPOSE)]
    files: list[str] = []
    for item in configured.split(os.pathsep):
        path = Path(item)
        files.append(str(path if path.is_absolute() else COMPOSE.parent / path))
    return files


def install(secret_dir: Path) -> Path:
    project = validate_project_name(os.environ.get("COMPOSE_PROJECT_NAME", "coire"))
    state_base = Path(os.environ.get("COIRE_STATE_ROOT", str(Path.home() / ".coire/projects")))
    if state_base.resolve().is_relative_to(ROOT):
        raise ReleaseInputError("release state must be outside the repository")
    project_root = state_base / project
    release_root = project_root / "releases"
    if project_root.is_symlink() or release_root.is_symlink():
        raise ReleaseInputError("release root must not be a symlink")
    release_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    release_root.chmod(0o700)
    release = Path(tempfile.mkdtemp(prefix="release-", dir=release_root))
    release.chmod(0o700)
    try:
        files = source_files()
        args: list[str] = []
        for file in files:
            args += ["-f", file]
        resolved: dict[str, Any] = json.loads(
            docker("compose", *args, "config", "--format", "json").stdout
        )
        cluster_source = Path(
            os.environ.get("COIRE_CLUSTER_CONFIG_DIR", str(ROOT / "deploy/cluster/generated"))
        )
        if cluster_source.is_symlink() or not cluster_source.is_dir():
            raise ReleaseInputError("cluster config source must be an existing directory")
        cluster_dest = release / "cluster"
        shutil.copytree(cluster_source, cluster_dest, symlinks=False)
        image_ids: dict[str, str] = {}
        for name, service in resolved["services"].items():
            image = service.get("image", "")
            if image.startswith("coire-") or "/coire-" in image:
                inspected = docker("image", "inspect", "--format", "{{.Id}}", image, check=False)
                if inspected.returncode != 0 or not inspected.stdout.startswith("sha256:"):
                    raise ReleaseInputError(f"missing built image for {name}; run coire-up --build")
                image_ids[name] = inspected.stdout.strip()
        manifest = release_manifest(
            resolved, secret_dir=secret_dir, cluster_dir=cluster_dest, image_ids=image_ids
        )
        (release / "compose.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        return release
    except BaseException:
        shutil.rmtree(release)
        raise


def preflight(release: Path) -> None:
    project = validate_project_name(os.environ.get("COMPOSE_PROJECT_NAME", "coire"))
    existing = docker(
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        "label=com.docker.compose.service=postgres",
    ).stdout.strip()
    if not existing:
        return
    result = docker(
        "compose",
        "-p",
        project,
        "-f",
        str(release / "compose.json"),
        "run",
        "--rm",
        "-T",
        "--no-deps",
        "coire-migrate",
        "-c",
        PROBE,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseInputError(
            "application-network PostgreSQL authentication failed; existing data was not changed"
        )


def recover_db_role(secret_dir: Path, backup_dir: Path) -> Path:
    """Back up the persisted database, then set only its application role credential."""
    project = validate_project_name(os.environ.get("COMPOSE_PROJECT_NAME", "coire"))
    containers = docker(
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        "label=com.docker.compose.service=postgres",
    ).stdout.splitlines()
    if len(containers) != 1:
        raise ReleaseInputError(
            "recovery requires exactly one running project PostgreSQL container"
        )
    password_path = secret_dir / "postgres_password"
    if not password_path.is_file() or password_path.is_symlink():
        raise ReleaseInputError("staged PostgreSQL credential is missing or unsafe")
    password = password_path.read_text()
    if not password:
        raise ReleaseInputError("staged PostgreSQL credential is empty")
    if backup_dir.is_symlink():
        raise ReleaseInputError("backup directory must not be a symlink")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    fd, backup_name = tempfile.mkstemp(prefix="coire-pre-recovery-", suffix=".dump", dir=backup_dir)
    backup = Path(backup_name)
    try:
        with os.fdopen(fd, "wb") as target:
            result = subprocess.run(
                ["docker", "exec", containers[0], "pg_dump", "-U", "coire", "-d", "coire", "-Fc"],
                stdout=target,
                stderr=subprocess.PIPE,
                check=False,
            )
        if result.returncode != 0 or backup.stat().st_size == 0:
            raise ReleaseInputError("database backup failed; role was not changed")
        sql = f"ALTER ROLE coire WITH PASSWORD '{password.replace(chr(39), chr(39) * 2)}';\n"
        altered = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                containers[0],
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "coire",
                "-d",
                "coire",
            ],
            input=sql,
            capture_output=True,
            text=True,
            check=False,
        )
        if altered.returncode != 0:
            raise ReleaseInputError("database role update failed; backup is retained")
        return backup
    except BaseException:
        if not backup.exists() or backup.stat().st_size == 0:
            backup.unlink(missing_ok=True)
        raise


def select_release(release: Path) -> None:
    project = validate_project_name(os.environ.get("COMPOSE_PROJECT_NAME", "coire"))
    state_base = Path(os.environ.get("COIRE_STATE_ROOT", str(Path.home() / ".coire/projects")))
    releases = (state_base / project / "releases").resolve()
    chosen = release.resolve()
    if chosen.parent != releases or not (chosen / "compose.json").is_file():
        raise ReleaseInputError("release is not owned by this Compose project")
    active = state_base / project / "current"
    candidate = active.with_name("current.pending")
    candidate.unlink(missing_ok=True)
    candidate.symlink_to(chosen)
    os.replace(candidate, active)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["install", "preflight", "recover-db-role", "select"])
    parser.add_argument("path", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "install":
            print(install(args.path))
        elif args.command == "preflight":
            preflight(args.path)
        elif args.command == "select":
            select_release(args.path)
        else:
            if not args.apply or args.backup_dir is None:
                raise ReleaseInputError("recovery requires --apply and --backup-dir")
            print(recover_db_role(args.path, args.backup_dir))
    except (ReleaseInputError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            print("release preparation failed; inspect Docker status", file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
