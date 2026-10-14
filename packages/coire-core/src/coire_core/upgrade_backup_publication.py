"""Host-only streaming backup publication into the existing service-owned catalog."""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import BinaryIO

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import BackupEvidence, UpgradeBackupPublication
from coire_core.upgrade_backups import verify_prepared_backup
from coire_core.upgrade_journal import PrivateUpgradeStore

MAX_BACKUP_BYTES = 4 * 1024**3
MAX_HEADER_BYTES = 8192
DESTINATION = Path("/opt/coire/upgrades/prepared/backups")


def publish_stream(reader: BinaryIO, root: Path) -> BackupEvidence:
    """One bounded JSON header followed by exact dump bytes; no archive paths or commands."""
    header = reader.readline(MAX_HEADER_BYTES + 1)
    if len(header) > MAX_HEADER_BYTES or not header.endswith(b"\n"):
        raise UpgradeConflict("Backup publication header exceeds its bound")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate backup publication field")
            result[key] = value
        return result

    try:
        json.loads(header, object_pairs_hook=unique)
        publication = UpgradeBackupPublication.model_validate_json(header)
    except (ValueError, RecursionError):
        raise UpgradeConflict("Backup publication header is malformed") from None
    evidence, validation = publication.evidence, publication.validation
    if evidence.size_bytes > MAX_BACKUP_BYTES:
        raise UpgradeConflict("Backup publication exceeds its tested size bound")
    if (
        evidence.backup_id,
        evidence.project,
        evidence.schema_head,
        evidence.sha256_digest,
        evidence.size_bytes,
        evidence.validated_at,
    ) != (
        validation.backup_id,
        validation.project,
        validation.schema_head,
        validation.dump_sha256,
        validation.size_bytes,
        validation.restored_at,
    ) or hashlib.sha256(
        validation.model_dump_json().encode()
    ).hexdigest() != evidence.validation_sha256:
        raise UpgradeConflict("Backup publication restore receipt differs")
    catalog = PrivateUpgradeStore(root)
    selected = catalog.path(evidence.backup_id)
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=root) as temporary:
        store = PrivateUpgradeStore(Path(temporary))
        path = store.path("database.dump")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        digest = hashlib.sha256()
        remaining = evidence.size_bytes
        with os.fdopen(fd, "wb") as output:
            while remaining:
                chunk = reader.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise UpgradeUnavailable("Backup publication ended before its declared bytes")
                remaining -= len(chunk)
                output.write(chunk)
                digest.update(chunk)
            if reader.read(1) or digest.hexdigest() != evidence.sha256_digest:
                raise UpgradeConflict("Backup publication dump content differs")
            output.flush()
            os.fsync(output.fileno())
        store.write("validation.json", validation, replace=False)
        store.write("backup.json", evidence, replace=False)
        store.sync()
        if selected.exists() or selected.is_symlink():
            verify_prepared_backup(
                root,
                evidence,
                project=evidence.project,
                schema_head=evidence.schema_head,
                schema_sha256=validation.schema_sha256,
            )
        else:
            # The catalog writer holds the existing private publication lock.
            os.rename(store.root, selected)
            catalog.sync()
        verify_prepared_backup(
            root,
            evidence,
            project=evidence.project,
            schema_head=evidence.schema_head,
            schema_sha256=validation.schema_sha256,
        )
    catalog.write("latest.json", evidence)
    return evidence


def main() -> int:
    if sys.argv[1:]:
        return 2
    try:
        catalog = PrivateUpgradeStore(DESTINATION)
        with catalog.ownership("publication.lock"):
            evidence = publish_stream(sys.stdin.buffer, DESTINATION)
    except (CoireError, OSError, ValueError):
        print("Protected backup publication failed", file=sys.stderr)
        return 2
    print(evidence.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
