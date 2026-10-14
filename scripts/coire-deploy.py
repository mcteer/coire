#!/usr/bin/env python3
"""Install a source-independent Compose release and validate existing database access."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from coire_core.deploy import (
    ReleaseInputError,
    deployment_mutex,
    owned_release_sha256,
    record_host_selection,
    release_manifest,
    validate_project_name,
    verify_host_deployment,
)
from coire_core.errors import CoireError
from coire_core.models.upgrades import (
    BackupEvidence,
    CoreBootstrapSeed,
    DeploymentOwnership,
    UpgradeBackupPublication,
    UpgradeBackupValidation,
    UpgradeComposeDatabase,
    UpgradeComposeMigration,
    UpgradeKind,
    UpgradeRelease,
    UpgradeReleaseRegister,
)
from coire_core.upgrade_artifacts import publish_prepared_release
from coire_core.upgrade_backups import verify_prepared_backup
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_core.upgrade_publication import publication_bundle

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


def docker(
    *args: str, check: bool = True, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=check,
        cwd=COMPOSE.parent,
        timeout=timeout,
    )


def _publication_volume(project: str, image_id: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9_-]{1,62}", project) is None:
        raise ReleaseInputError("container catalog publication project is invalid")
    if re.fullmatch(r"sha256:[a-f0-9]{64}", image_id) is None:
        raise ReleaseInputError("publisher requires an already verified immutable image ID")
    inspected = docker("image", "inspect", image_id, check=False, timeout=10)
    volume = f"{project}_coire-upgrade-prepared"
    volume_inspected = docker("volume", "inspect", volume, check=False, timeout=10)
    if (
        len(inspected.stdout.encode()) > 1024 * 1024
        or len(volume_inspected.stdout.encode()) > 1024 * 1024
    ):
        raise ReleaseInputError("publication resource metadata exceeds its bound")
    try:
        images, volumes = json.loads(inspected.stdout), json.loads(volume_inspected.stdout)
        if (
            inspected.returncode
            or volume_inspected.returncode
            or len(images) != 1
            or len(volumes) != 1
        ):
            raise ValueError("missing publication resource")
        image, catalog = images[0], volumes[0]
        if (
            image.get("Id"),
            image.get("Os"),
            image.get("Architecture"),
            image.get("Config", {}).get("User"),
        ) != (image_id, "linux", "arm64", "65532:65532"):
            raise ValueError("publisher identity differs")
        if (
            catalog.get("Name") != volume
            or catalog.get("Labels", {}).get("com.docker.compose.project") != project
        ):
            raise ValueError("catalog project differs")
    except (ValueError, TypeError, AttributeError, IndexError):
        raise ReleaseInputError(
            "publisher image or existing catalog ownership is invalid"
        ) from None
    return volume


def _publish_container_catalog(
    release: UpgradeRelease,
    artifacts: dict[str, Path],
    *,
    image_id: str,
) -> UpgradeReleaseRegister:
    """Trusted host-only transfer; the service UID writes its existing private catalog volume."""
    project = release.manifest.project or os.environ.get("COMPOSE_PROJECT_NAME", "coire")
    volume = _publication_volume(project, image_id)
    bundle = publication_bundle(release, artifacts)
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=64",
            "--memory=512m",
            "--cpus=0.5",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=128m,mode=1777",
            "--mount",
            f"type=volume,source={volume},target=/opt/coire/upgrades/prepared",
            "--entrypoint",
            "/app/.venv/bin/python3",
            image_id,
            "-m",
            "coire_core.upgrade_publication",
        ],
        input=bundle.model_dump_json(),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode or len(result.stdout.encode()) > 4096:
        raise ReleaseInputError("service-owned catalog publication failed")
    try:
        receipt = UpgradeReleaseRegister.model_validate_json(result.stdout)
    except ValueError:
        raise ReleaseInputError("catalog publication receipt is malformed") from None
    if (receipt.prepared_artifact_id, receipt.expected_sha256) != (
        release.prepared_artifact_id,
        release.manifest_sha256,
    ):
        raise ReleaseInputError("catalog publication receipt differs from its accepted bytes")
    return receipt


def publish_container_catalog(
    release: UpgradeRelease,
    artifacts: dict[str, Path],
    *,
    image_id: str,
) -> UpgradeReleaseRegister:
    try:
        return _publish_container_catalog(release, artifacts, image_id=image_id)
    except (OSError, subprocess.TimeoutExpired):
        raise ReleaseInputError("service-owned catalog publication is unavailable") from None


def publish_bootstrap_intent(seed: CoreBootstrapSeed) -> CoreBootstrapSeed:
    """Transfer only intent into existing private volumes, without network or readiness."""
    project = seed.source.manifest.project
    if project != validate_project_name(os.environ.get("COMPOSE_PROJECT_NAME", "coire")):
        raise ReleaseInputError("bootstrap intent belongs to another deployment project")
    prepared = _publication_volume(project, seed.executor_image_id)
    state = f"{project}_coire-upgrade-state"
    inspected = docker("volume", "inspect", state, check=False, timeout=10)
    try:
        if inspected.returncode or len(inspected.stdout.encode()) > 1024 * 1024:
            raise ValueError("state volume unavailable")
        volumes = json.loads(inspected.stdout)
        if (
            len(volumes) != 1
            or volumes[0].get("Name") != state
            or volumes[0].get("Labels", {}).get("com.docker.compose.project") != project
        ):
            raise ValueError("state volume differs")
    except (ValueError, TypeError, AttributeError, IndexError):
        raise ReleaseInputError(
            "bootstrap requires its existing service-owned state volume"
        ) from None
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--pull=never",
                "--network=none",
                "--read-only",
                "--user",
                "65532:65532",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--pids-limit=64",
                "--memory=512m",
                "--cpus=0.5",
                "--env",
                f"UPGRADE_PROJECT={project}",
                "--mount",
                f"type=volume,source={prepared},target=/opt/coire/upgrades/prepared,readonly",
                "--mount",
                f"type=volume,source={state},target=/opt/coire/upgrades/state",
                "--entrypoint",
                "/app/.venv/bin/python3",
                seed.executor_image_id,
                "-m",
                "coire_core.upgrade_bootstrap_publication",
            ],
            input=seed.model_dump_json(),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if result.returncode or len(result.stdout.encode()) > 1024 * 1024:
            raise ReleaseInputError("administrative bootstrap intent publication failed")
        receipt = CoreBootstrapSeed.model_validate_json(result.stdout)
        if receipt != seed:
            raise ReleaseInputError(
                "administrative bootstrap publication changed its immutable intent"
            )
        return receipt
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise ReleaseInputError("administrative bootstrap publication is unavailable") from None


def publish_backup_catalog(
    root: Path, evidence: BackupEvidence, *, image_id: str
) -> BackupEvidence:
    """Stream a verified private dump to the fixed service-owned catalog, without a shell."""
    volume = _publication_volume(evidence.project, image_id)
    store = PrivateUpgradeStore(PrivateUpgradeStore(root).path(evidence.backup_id))
    validation = store.read("validation.json", UpgradeBackupValidation)
    if validation is None:
        raise ReleaseInputError("backup restore validation is absent")
    verify_prepared_backup(
        root,
        evidence,
        project=evidence.project,
        schema_head=evidence.schema_head,
        schema_sha256=validation.schema_sha256,
    )
    if (
        evidence.size_bytes > 4 * 1024**3
        or shutil.disk_usage(root).free < evidence.size_bytes + 1024**3
    ):
        raise ReleaseInputError("backup transfer exceeds its tested size or staging headroom")
    publication = UpgradeBackupPublication(evidence=evidence, validation=validation)
    with (
        tempfile.TemporaryFile(dir=root) as packet,
        tempfile.TemporaryFile() as output,
        tempfile.TemporaryFile() as errors,
    ):
        packet.write(publication.model_dump_json().encode() + b"\n")
        fd = os.open(store.path("database.dump"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            details = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_mode & 0o077
                or details.st_nlink != 1
                or details.st_size != evidence.size_bytes
            ):
                raise ReleaseInputError("backup transfer source ownership differs")
            remaining = evidence.size_bytes
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ReleaseInputError("backup transfer source is truncated")
                packet.write(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise ReleaseInputError("backup transfer source grew")
        packet.flush()
        packet.seek(0)
        verify_prepared_backup(
            root,
            evidence,
            project=evidence.project,
            schema_head=evidence.schema_head,
            schema_sha256=validation.schema_sha256,
        )
        try:
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-i",
                    "--pull=never",
                    "--network=none",
                    "--read-only",
                    "--user=65532:65532",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges:true",
                    "--pids-limit=64",
                    "--memory=512m",
                    "--cpus=0.5",
                    "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=128m,mode=1777",
                    "--mount",
                    f"type=volume,source={volume},target=/opt/coire/upgrades/prepared",
                    "--entrypoint=/app/.venv/bin/python3",
                    image_id,
                    "-m",
                    "coire_core.upgrade_backup_publication",
                ],
                stdin=packet,
                stdout=output,
                stderr=errors,
                check=False,
                timeout=900,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ReleaseInputError("backup publication acknowledgment is unavailable") from None
        output.seek(0)
        response = output.read(4097)
        if result.returncode or len(response) > 4096:
            raise ReleaseInputError("service-owned backup publication failed")
        try:
            accepted = BackupEvidence.model_validate_json(response)
        except ValueError:
            raise ReleaseInputError("backup publication receipt is malformed") from None
        if accepted != evidence:
            raise ReleaseInputError("backup publication receipt differs from verified bytes")
        return accepted


def source_files() -> list[str]:
    configured = os.environ.get("COMPOSE_FILE")
    if not configured:
        return [str(COMPOSE)]
    files: list[str] = []
    for item in configured.split(os.pathsep):
        path = Path(item)
        files.append(str(path if path.is_absolute() else COMPOSE.parent / path))
    return files


def verify_host_release_binding(release: UpgradeRelease) -> None:
    if release.manifest.kind is not UpgradeKind.CONTROL_PLANE:
        return
    project, key = release.manifest.project, release.manifest.host_release_key
    if project is None or key is None:
        raise ReleaseInputError("prepared Core release requires a source-independent host key")
    state_root = Path(os.environ.get("COIRE_STATE_ROOT", str(Path.home() / ".coire/projects")))
    if owned_release_sha256(state_root, project, key) != release.manifest.compose_sha256:
        raise ReleaseInputError(
            "prepared Core host release content differs from its immutable artifact"
        )


def verify_upgrade_images(
    release: UpgradeRelease, artifacts: dict[str, Path] | None = None
) -> None:
    """Prepared Core images must already exist locally with exact platform and digest."""
    if release.manifest.kind is not UpgradeKind.CONTROL_PLANE:
        return
    images = dict.fromkeys(service.image_id for service in release.manifest.services)
    if artifacts is not None:
        bundle = publication_bundle(release, artifacts)
        compose = next((item for item in bundle.artifacts if item.key == "compose.json"), None)
        if compose is None:
            raise ReleaseInputError("prepared Core Compose artifact is absent")
        try:
            document = json.loads(base64.b64decode(compose.content_base64, validate=True))
            database = document["services"].get("postgres")
            if database is not None:
                images[UpgradeComposeDatabase.model_validate(database).image] = None
            migration = document["services"].get("coire-migrate")
            if migration is not None:
                images[UpgradeComposeMigration.model_validate(migration).image] = None
        except (ValueError, TypeError, KeyError):
            raise ReleaseInputError(
                "prepared database or migration image configuration is invalid"
            ) from None
    for image_id in images:
        try:
            inspected = docker("image", "inspect", image_id, check=False, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise ReleaseInputError("prepared upgrade image inspection is unavailable") from None
        if inspected.returncode != 0 or len(inspected.stdout.encode()) > 1024 * 1024:
            raise ReleaseInputError("prepared upgrade image is absent or unreadable")
        try:
            rows = json.loads(inspected.stdout)
        except ValueError:
            raise ReleaseInputError("prepared upgrade image metadata is corrupt") from None
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
            or rows[0].get("Id") != image_id
            or rows[0].get("Os") != "linux"
            or rows[0].get("Architecture") != "arm64"
        ):
            raise ReleaseInputError("prepared upgrade image identity or platform differs")


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
            if name == "postgres" or image.startswith("coire-") or "/coire-" in image:
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
    if (state_base / project / "upgrades/coordination/ownership.json").exists():
        record_host_selection(state_base, project, release)
        return
    active = state_base / project / "current"
    candidate = active.with_name("current.pending")
    candidate.unlink(missing_ok=True)
    candidate.symlink_to(chosen)
    os.replace(candidate, active)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "install",
            "preflight",
            "recover-db-role",
            "select",
            "prepare-upgrade",
            "prepare-backup",
            "prepare-bootstrap",
        ],
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--release-document", type=Path)
    parser.add_argument(
        "--publisher-image",
        help="immutable verified executor image; publish to its service-owned catalog volume",
    )
    parser.add_argument("--artifact", action="append", default=[], metavar="KEY=PATH")
    args = parser.parse_args()
    try:
        if args.command == "prepare-bootstrap":
            if args.path.resolve().is_relative_to(ROOT):
                raise ReleaseInputError("bootstrap document must be private and outside Git")
            seed = PrivateUpgradeStore(args.path.parent).read(args.path.name, CoreBootstrapSeed)
            if seed is None:
                raise ReleaseInputError("administrative bootstrap seed document is absent")
            print(publish_bootstrap_intent(seed).model_dump_json())
        elif args.command == "prepare-backup":
            from coire_api.upgrades.backup_preparation import prepare_backup

            if args.path.resolve().is_relative_to(ROOT):
                raise ReleaseInputError("backup catalog must be private and outside Git")
            project = validate_project_name(os.environ.get("COMPOSE_PROJECT_NAME", "coire"))
            state_root = Path(
                os.environ.get("COIRE_STATE_ROOT", str(Path.home() / ".coire/projects"))
            )
            coordination = PrivateUpgradeStore(state_root / project / "upgrades/coordination")
            baseline = coordination.read("ownership.json", DeploymentOwnership)
            if baseline is None or baseline.project != project or baseline.state != "committed":
                raise ReleaseInputError(
                    "backup preparation requires reconciled deployment ownership"
                )
            with deployment_mutex(coordination, project, baseline.owner_identity):
                if coordination.read("ownership.json", DeploymentOwnership) != baseline:
                    raise ReleaseInputError("deployment changed before backup preparation")
                evidence = prepare_backup(args.path, project)
                if args.publisher_image is not None:
                    evidence = publish_backup_catalog(
                        args.path, evidence, image_id=args.publisher_image
                    )
            print(evidence.model_dump_json())
        elif args.command == "prepare-upgrade":
            if args.release_document is None or args.path.resolve().is_relative_to(ROOT):
                raise ReleaseInputError(
                    "preparation requires a private release document and catalog outside Git"
                )
            document_store = PrivateUpgradeStore(args.release_document.parent)
            release = document_store.read(args.release_document.name, UpgradeRelease)
            if release is None:
                raise ReleaseInputError("prepared release document is absent")
            artifacts: dict[str, Path] = {}
            for item in args.artifact:
                key, separator, path = item.partition("=")
                if not separator or not path or key in artifacts:
                    raise ReleaseInputError("artifact binding must be a unique KEY=PATH")
                artifacts[key] = Path(path)
            verify_host_release_binding(release)
            verify_upgrade_images(release, artifacts)
            request = (
                publish_container_catalog(release, artifacts, image_id=args.publisher_image)
                if args.publisher_image is not None
                else publish_prepared_release(args.path, release, artifacts)
            )
            print(request.model_dump_json())
        elif args.command == "install":
            print(install(args.path))
        elif args.command == "preflight":
            preflight(args.path)
        elif args.command == "select":
            verify_host_deployment(
                Path(os.environ.get("COIRE_STATE_ROOT", str(Path.home() / ".coire/projects"))),
                validate_project_name(os.environ.get("COMPOSE_PROJECT_NAME", "coire")),
            )
            select_release(args.path)
        else:
            if not args.apply or args.backup_dir is None:
                raise ReleaseInputError("recovery requires --apply and --backup-dir")
            verify_host_deployment(
                Path(os.environ.get("COIRE_STATE_ROOT", str(Path.home() / ".coire/projects"))),
                validate_project_name(os.environ.get("COMPOSE_PROJECT_NAME", "coire")),
            )
            print(recover_db_role(args.path, args.backup_dir))
    except (ReleaseInputError, CoireError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            print("release preparation failed; inspect Docker status", file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
