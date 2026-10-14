"""Binary backup transfer accepts exact protected bytes and no received filesystem paths."""

import hashlib
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    BackupEvidence,
    UpgradeBackupPublication,
    UpgradeBackupValidation,
)
from coire_core.upgrade_backup_publication import publish_stream
from coire_core.upgrade_backups import verify_prepared_backup


def publication() -> tuple[UpgradeBackupPublication, bytes]:
    payload = b"PGDMP retained fixture bytes"
    now = datetime.now(UTC)
    validation = UpgradeBackupValidation(
        backup_id="backup-fixture",
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
        size_bytes=len(payload),
        validated_at=now,
        validation_sha256=hashlib.sha256(validation.model_dump_json().encode()).hexdigest(),
    )
    return UpgradeBackupPublication(evidence=evidence, validation=validation), payload


def test_publication_and_lost_ack_replay_preserve_exact_files(tmp_path: Path) -> None:
    value, payload = publication()
    stream = value.model_dump_json().encode() + b"\n" + payload
    root = tmp_path / "backups"
    assert publish_stream(io.BytesIO(stream), root) == value.evidence
    selected = root / value.evidence.backup_id / "database.dump"
    original = selected.stat()
    assert selected.read_bytes() == payload
    assert original.st_mode & 0o077 == 0
    assert publish_stream(io.BytesIO(stream), root) == value.evidence
    assert selected.stat().st_ino == original.st_ino
    assert (
        verify_prepared_backup(
            root,
            value.evidence,
            project="coire",
            schema_head=value.evidence.schema_head,
            schema_sha256=value.validation.schema_sha256,
        )
        == value.validation
    )


@pytest.mark.parametrize(
    "fault", ["short", "extra", "content", "path", "duplicate", "large", "receipt"]
)
def test_invalid_stream_never_selects_a_backup(tmp_path: Path, fault: str) -> None:
    value, payload = publication()
    header = value.model_dump_json().encode() + b"\n"
    if fault == "short":
        payload = payload[:-1]
    elif fault == "extra":
        payload += b"x"
    elif fault == "content":
        payload = b"x" * len(payload)
    elif fault == "path":
        header = header.replace(b'"backup-fixture"', b'"../escape"')
    elif fault == "duplicate":
        header = header.replace(b'"protocol":1', b'"protocol":1,"protocol":1', 1)
    elif fault == "large":
        header = b"x" * 8193 + b"\n"
    else:
        header = header.replace(value.evidence.validation_sha256.encode(), b"f" * 64)
    with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
        publish_stream(io.BytesIO(header + payload), tmp_path / "backups")
    assert not (tmp_path / "backups" / "latest.json").exists()
    assert not (tmp_path / "backups" / value.evidence.backup_id).exists()


def test_existing_immutable_backup_cannot_be_replaced_with_new_bytes(tmp_path: Path) -> None:
    value, payload = publication()
    root = tmp_path / "backups"
    publish_stream(io.BytesIO(value.model_dump_json().encode() + b"\n" + payload), root)
    changed = b"x" * len(payload)
    digest = hashlib.sha256(changed).hexdigest()
    validation = value.validation.model_copy(update={"dump_sha256": digest})
    evidence = value.evidence.model_copy(
        update={
            "sha256_digest": digest,
            "validation_sha256": hashlib.sha256(validation.model_dump_json().encode()).hexdigest(),
        }
    )
    alternate = UpgradeBackupPublication(evidence=evidence, validation=validation)
    with pytest.raises(UpgradeConflict):
        publish_stream(io.BytesIO(alternate.model_dump_json().encode() + b"\n" + changed), root)
    assert (root / value.evidence.backup_id / "database.dump").read_bytes() == payload
