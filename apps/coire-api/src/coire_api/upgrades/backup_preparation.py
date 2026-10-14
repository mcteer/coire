"""Administrative backup preparation; never imported by the runtime executor."""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from coire_api.upgrades.schema_identity import _STRUCTURE
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import BackupEvidence, UpgradeBackupValidation
from coire_core.upgrade_backups import verify_prepared_backup
from coire_core.upgrade_journal import PrivateUpgradeStore


def command(*arguments: str, timeout: float = 120) -> str:
    """Fixed admin Docker commands return bounded metadata and never diagnostic secrets."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            subprocess.run(
                ["docker", *arguments], stdout=output, stderr=errors, check=True, timeout=timeout
            )
        except (subprocess.SubprocessError, OSError):
            raise UpgradeUnavailable("Backup preparation Docker operation failed") from None
        output.seek(0)
        payload = output.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024:
            raise UpgradeUnavailable("Backup preparation metadata exceeds its bound")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            raise UpgradeUnavailable("Backup preparation metadata is malformed") from None


def schema_identity(container_id: str) -> tuple[str, str]:
    """Use the same read-only structural digest as activation, through local admin access."""
    base = (
        "exec",
        container_id,
        "psql",
        "-X",
        "-q",
        "-A",
        "-t",
        "-U",
        "coire",
        "-d",
        "coire",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
    )
    head = command(*base, "SELECT version_num FROM alembic_version LIMIT 2").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", head) is None:
        raise UpgradeConflict("Backup Source must have one known schema revision")
    query = (
        "SELECT json_build_array(kind, identity, definition)::text FROM ("
        + _STRUCTURE
        + ') objects ORDER BY kind COLLATE "C", identity COLLATE "C", definition COLLATE "C"'
    )
    rows = command(*base, "SET statement_timeout='5s'; " + query)
    try:
        items = [json.loads(row) for row in rows.splitlines()]
        if len(items) > 32768 or any(
            not isinstance(row, list)
            or len(row) != 3
            or any(not isinstance(item, str) for item in row)
            for row in items
        ):
            raise ValueError("schema inventory differs")
        payload = json.dumps(items, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError("schema inventory exceeds bound")
    except (ValueError, RecursionError):
        raise UpgradeUnavailable("Backup schema inventory is malformed") from None
    return head, hashlib.sha256(payload).hexdigest()


def prepare_backup(root: Path, project: str) -> BackupEvidence:
    """Dump Source and restore it only into a disposable, disconnected PostgreSQL 17."""
    if re.fullmatch(r"[a-z][a-z0-9_-]{1,62}", project) is None:
        raise UpgradeConflict("Backup project is invalid")
    sources = command(
        "ps",
        "-q",
        "--no-trunc",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        "label=com.docker.compose.service=postgres",
    ).splitlines()
    if len(sources) != 1 or re.fullmatch(r"[a-f0-9]{64}", sources[0]) is None:
        raise UpgradeConflict("Backup requires exactly one running project PostgreSQL")
    source = sources[0]
    inspected = json.loads(command("inspect", source))
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise UpgradeConflict("Backup PostgreSQL identity is unavailable")
    identity = inspected[0]
    image = identity.get("Image")
    if (
        identity.get("Id") != source
        or identity.get("State", {}).get("Running") is not True
        or identity.get("Config", {}).get("Labels", {}).get("com.docker.compose.project") != project
        or identity.get("Config", {}).get("Labels", {}).get("com.docker.compose.service")
        != "postgres"
        or "PG_MAJOR=17" not in identity.get("Config", {}).get("Env", [])
        or not isinstance(image, str)
        or re.fullmatch(r"sha256:[a-f0-9]{64}", image) is None
    ):
        raise UpgradeConflict("Backup Source is outside the supported PostgreSQL 17 project")
    image_record = json.loads(command("image", "inspect", image))
    if len(image_record) != 1 or (
        image_record[0].get("Os"),
        image_record[0].get("Architecture"),
    ) != ("linux", "arm64"):
        raise UpgradeConflict("Backup PostgreSQL image platform differs")
    head, digest = schema_identity(source)
    created_at = datetime.now(UTC)
    catalog = PrivateUpgradeStore(root)
    key = "backup-" + uuid4().hex
    store = PrivateUpgradeStore(catalog.path(key))
    dump = store.path("database.dump")
    fd = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output, tempfile.TemporaryFile() as errors:
        try:
            subprocess.run(
                ["docker", "exec", source, "pg_dump", "-U", "coire", "-d", "coire", "-Fc"],
                stdout=output,
                stderr=errors,
                check=True,
                timeout=900,
            )
            output.flush()
            os.fsync(output.fileno())
        except (subprocess.SubprocessError, OSError):
            raise UpgradeUnavailable(
                "Source database backup failed; partial dump remains private"
            ) from None
    size = dump.stat().st_size
    if size <= 0:
        raise UpgradeUnavailable("Source database backup is empty")
    with dump.open("rb") as captured:
        dump_digest = hashlib.file_digest(captured, "sha256").hexdigest()
    name = "coire-upgrade-backup-" + uuid4().hex
    candidate = command(
        "create",
        "--pull=never",
        "--name",
        name,
        "--network=none",
        "--user=999:999",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=128",
        "--memory=2g",
        "--cpus=1",
        "--restart=no",
        "--label",
        "coire.upgrade.backup.id=" + key,
        "--tmpfs=/var/lib/postgresql/data:rw,size=4294967296,uid=999,gid=999,mode=0700",
        "--tmpfs=/var/run/postgresql:rw,size=16777216,uid=999,gid=999,mode=0755",
        "--tmpfs=/tmp:rw,size=16777216",
        "-e",
        "POSTGRES_USER=coire",
        "-e",
        "POSTGRES_DB=coire",
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        image,
    ).strip()
    if re.fullmatch(r"[a-f0-9]{64}", candidate) is None:
        raise UpgradeUnavailable("Isolated restore container acknowledgment is uncertain")

    def verify_candidate() -> None:
        actual = json.loads(command("inspect", candidate))
        if (
            not isinstance(actual, list)
            or len(actual) != 1
            or actual[0].get("Id") != candidate
            or actual[0].get("Name") != "/" + name
            or actual[0].get("Image") != image
            or actual[0].get("Config", {}).get("Labels", {}).get("coire.upgrade.backup.id") != key
            or actual[0].get("HostConfig", {}).get("NetworkMode") != "none"
            or actual[0].get("HostConfig", {}).get("ReadonlyRootfs") is not True
            or any(mount.get("Type") != "tmpfs" for mount in actual[0].get("Mounts", []))
        ):
            raise UpgradeConflict("Isolated restore container ownership differs")

    verify_candidate()
    try:
        command("start", candidate)
        deadline = time.monotonic() + 30
        while True:
            try:
                command(
                    "exec",
                    candidate,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "coire",
                    "-d",
                    "coire",
                    timeout=2,
                )
                break
            except UpgradeUnavailable:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        with dump.open("rb") as input_dump, tempfile.TemporaryFile() as errors:
            try:
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        candidate,
                        "pg_restore",
                        "--exit-on-error",
                        "--no-owner",
                        "-U",
                        "coire",
                        "-d",
                        "coire",
                    ],
                    stdin=input_dump,
                    stdout=errors,
                    stderr=errors,
                    check=True,
                    timeout=900,
                )
            except (subprocess.SubprocessError, OSError):
                raise UpgradeUnavailable("Isolated backup restore failed") from None
        if schema_identity(candidate) != (head, digest) or schema_identity(source) != (
            head,
            digest,
        ):
            raise UpgradeConflict("Restored or deployed Source schema differs from captured backup")
        validation = UpgradeBackupValidation(
            backup_id=key,
            project=project,
            schema_head=head,
            schema_sha256=digest,
            dump_sha256=dump_digest,
            size_bytes=size,
            restore_container_id=candidate,
            restore_container_name=name,
            postgres_image_id=image,
            restored_at=datetime.now(UTC),
        )
        evidence = BackupEvidence(
            backup_id=key,
            project=project,
            schema_head=head,
            created_at=created_at,
            sha256_digest=dump_digest,
            size_bytes=size,
            validated_at=validation.restored_at,
            validation_sha256=hashlib.sha256(validation.model_dump_json().encode()).hexdigest(),
        )
        store.write("validation.json", validation, replace=False)
        store.write("backup.json", evidence, replace=False)
        store.sync()
        verify_prepared_backup(
            root, evidence, project=project, schema_head=head, schema_sha256=digest
        )
    finally:
        verify_candidate()
        command("stop", "--time=5", candidate)
        command("rm", candidate)
    catalog.write("latest.json", evidence)
    return evidence
