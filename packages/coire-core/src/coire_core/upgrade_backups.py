"""Read-only verification of protected, operator-prepared database backups."""

import hashlib
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import BackupEvidence, UpgradeBackupValidation
from coire_core.upgrade_journal import PrivateUpgradeStore


def verify_prepared_backup(
    root: Path,
    evidence: BackupEvidence,
    *,
    project: str,
    schema_head: str,
    schema_sha256: str,
    now: datetime | None = None,
) -> UpgradeBackupValidation:
    """A receipt grants nothing unless its retained dump and restored schema still agree."""
    clock = now or datetime.now(UTC)
    if (
        evidence.project != project
        or evidence.schema_head != schema_head
        or not clock - timedelta(hours=24) <= evidence.created_at <= evidence.validated_at <= clock
    ):
        raise UpgradeConflict("Backup project, deployed schema or freshness differs")
    if not root.is_dir() or root.is_symlink():
        raise UpgradeUnavailable("Protected backup catalog is absent")
    catalog = PrivateUpgradeStore(root)
    directory = catalog.path(evidence.backup_id)
    if not directory.is_dir() or directory.is_symlink():
        raise UpgradeUnavailable("Protected backup is absent")
    store = PrivateUpgradeStore(directory)
    retained = store.read("backup.json", BackupEvidence)
    validation = store.read("validation.json", UpgradeBackupValidation)
    if retained != evidence or validation is None:
        raise UpgradeConflict("Protected backup receipt differs")
    if (
        validation.backup_id,
        validation.project,
        validation.schema_head,
        validation.schema_sha256,
        validation.dump_sha256,
        validation.size_bytes,
        validation.restored_at,
    ) != (
        evidence.backup_id,
        project,
        schema_head,
        schema_sha256,
        evidence.sha256_digest,
        evidence.size_bytes,
        evidence.validated_at,
    ) or hashlib.sha256(
        validation.model_dump_json().encode()
    ).hexdigest() != evidence.validation_sha256:
        raise UpgradeConflict("Isolated backup restore validation differs")
    path = store.path("database.dump")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise UpgradeUnavailable("Protected backup dump cannot be opened safely") from None
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o077
            or before.st_nlink != 1
            or before.st_size != evidence.size_bytes
        ):
            raise UpgradeConflict("Protected backup dump ownership or size differs")
        digest = hashlib.sha256()
        count = 0
        with os.fdopen(fd, "rb", closefd=False) as handle:
            while chunk := handle.read(1024 * 1024):
                count += len(chunk)
                if count > before.st_size:
                    raise UpgradeUnavailable("Protected backup dump grew during verification")
                digest.update(chunk)
        after = os.fstat(fd)
        selected = path.lstat()

        def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if identity(before) != identity(after) or identity(before) != identity(selected):
            raise UpgradeUnavailable("Protected backup dump changed during verification")
        if count != evidence.size_bytes or digest.hexdigest() != evidence.sha256_digest:
            raise UpgradeConflict("Protected backup dump content differs")
    finally:
        os.close(fd)
    if (
        store.read("backup.json", BackupEvidence) != retained
        or store.read("validation.json", UpgradeBackupValidation) != validation
    ):
        raise UpgradeUnavailable("Protected backup receipts changed during verification")
    return validation


def select_prepared_backup(
    root: Path, *, project: str, schema_head: str, schema_sha256: str
) -> BackupEvidence:
    """Read a trusted preparation pointer; accepted plans retain its immutable receipt."""
    if not root.is_dir() or root.is_symlink():
        raise UpgradeUnavailable("Protected backup catalog is absent")
    store = PrivateUpgradeStore(root)
    evidence = store.read("latest.json", BackupEvidence)
    if evidence is None:
        raise UpgradeUnavailable("No restore-validated backup has been prepared")
    verify_prepared_backup(
        root, evidence, project=project, schema_head=schema_head, schema_sha256=schema_sha256
    )
    if store.read("latest.json", BackupEvidence) != evidence:
        raise UpgradeConflict("Prepared backup selection changed during verification")
    return evidence
