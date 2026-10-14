"""Metadata alone cannot authorize DDL when the retained dump or restore proof differs."""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import BackupEvidence, UpgradeBackupValidation
from coire_core.upgrade_backups import verify_prepared_backup
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "dump",
        "size",
        "schema",
        "receipt",
        "stale",
        "future",
        "linked",
        "permissions",
        "missing",
    ],
)
def test_protected_backup_requires_actual_dump_and_matching_isolated_restore(
    tmp_path: Path, fault: str | None
) -> None:
    now = datetime.now(UTC)
    PrivateUpgradeStore(tmp_path / "backups")
    directory = tmp_path / "backups" / "backup-source"
    store = PrivateUpgradeStore(directory)
    payload = b"PGDMP isolated fixture bytes"
    dump = directory / "database.dump"
    dump.write_bytes(payload)
    dump.chmod(0o600)
    validation = UpgradeBackupValidation(
        backup_id="backup-source",
        project="coire",
        schema_head="0034_upgrades",
        schema_sha256="a" * 64,
        dump_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        restore_container_id="b" * 64,
        restore_container_name="coire-upgrade-backup-" + "c" * 32,
        postgres_image_id="sha256:" + "d" * 64,
        restored_at=now,
    )
    evidence = BackupEvidence(
        backup_id=validation.backup_id,
        project=validation.project,
        schema_head=validation.schema_head,
        created_at=now - timedelta(seconds=1),
        sha256_digest=validation.dump_sha256,
        size_bytes=validation.size_bytes,
        validated_at=now,
        validation_sha256=hashlib.sha256(validation.model_dump_json().encode()).hexdigest(),
    )
    store.write("backup.json", evidence)
    store.write("validation.json", validation)
    if fault == "dump":
        dump.write_bytes(b"x" * len(payload))
    elif fault == "size":
        dump.write_bytes(payload + b"x")
    elif fault == "receipt":
        store.write("validation.json", validation.model_copy(update={"dump_sha256": "f" * 64}))
    elif fault == "stale":
        evidence = evidence.model_copy(update={"created_at": now - timedelta(hours=25)})
    elif fault == "future":
        evidence = evidence.model_copy(update={"validated_at": now + timedelta(seconds=1)})
    elif fault == "linked":
        retained = directory / "original.dump"
        dump.rename(retained)
        dump.symlink_to(retained)
    elif fault == "permissions":
        dump.chmod(0o644)
    elif fault == "missing":
        dump.unlink()
    arguments = {
        "project": "coire",
        "schema_head": "0034_upgrades",
        "schema_sha256": "f" * 64 if fault == "schema" else "a" * 64,
        "now": now,
    }
    if fault is None:
        assert verify_prepared_backup(tmp_path / "backups", evidence, **arguments) == validation
    else:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
            verify_prepared_backup(tmp_path / "backups", evidence, **arguments)
