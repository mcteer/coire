"""Host-only bounded stdin publication into a non-root service-owned prepared volume."""

import base64
import binascii
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import BinaryIO

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    UpgradePublicationArtifact,
    UpgradePublicationBundle,
    UpgradeRelease,
    UpgradeReleaseRegister,
)
from coire_core.upgrade_artifacts import publish_prepared_release
from coire_core.upgrade_journal import PrivateUpgradeStore

MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_PUBLICATION_BYTES = 64 * 1024 * 1024
DESTINATION = Path("/opt/coire/upgrades/prepared")


def publication_bundle(
    release: UpgradeRelease, artifacts: dict[str, Path]
) -> UpgradePublicationBundle:
    """Host reads only private operator-owned regular files; no received path is forwarded."""
    if set(artifacts) != set(release.manifest.artifact_sha256s):
        raise UpgradeConflict("Publication artifact set differs from the accepted manifest")
    result = []
    for key, path in artifacts.items():
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            raise UpgradeUnavailable("Publication artifact is unavailable") from None
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or before.st_size > MAX_ARTIFACT_BYTES
            ):
                raise UpgradeConflict("Publication artifact ownership or size differs")
            with os.fdopen(fd, "rb", closefd=False) as reader:
                content = reader.read(MAX_ARTIFACT_BYTES + 1)
            after = os.fstat(fd)
            current = path.lstat()
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise UpgradeConflict("Publication artifact changed during reading")
            if (
                len(content) > MAX_ARTIFACT_BYTES
                or hashlib.sha256(content).hexdigest() != release.manifest.artifact_sha256s[key]
            ):
                raise UpgradeConflict("Publication artifact content differs")
            result.append(
                UpgradePublicationArtifact(
                    key=key, content_base64=base64.b64encode(content).decode("ascii")
                )
            )
        finally:
            os.close(fd)
    bundle = UpgradePublicationBundle(release=release, artifacts=tuple(result))
    if len(bundle.model_dump_json().encode()) > MAX_PUBLICATION_BYTES:
        raise UpgradeConflict("Publication bundle exceeds its bound")
    return bundle


def publish_stream(reader: BinaryIO, root: Path) -> UpgradeReleaseRegister:
    """Decode metadata/code artifacts into private temporary files, then publish immutably."""
    payload = reader.read(MAX_PUBLICATION_BYTES + 1)
    if len(payload) > MAX_PUBLICATION_BYTES:
        raise UpgradeConflict("Publication bundle exceeds its bound")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate publication field")
            result[key] = value
        return result

    try:
        json.loads(payload, object_pairs_hook=unique)
        bundle = UpgradePublicationBundle.model_validate_json(payload)
    except ValueError:
        raise UpgradeConflict("Publication bundle is malformed") from None
    with tempfile.TemporaryDirectory(prefix="coire-upgrade-publication-") as temporary:
        stage = PrivateUpgradeStore(Path(temporary))
        artifacts: dict[str, Path] = {}
        total = 0
        for item in bundle.artifacts:
            try:
                content = base64.b64decode(item.content_base64, validate=True)
            except (ValueError, binascii.Error):
                raise UpgradeConflict("Publication artifact encoding is malformed") from None
            total += len(content)
            if (
                len(content) > MAX_ARTIFACT_BYTES
                or total > MAX_PUBLICATION_BYTES
                or total > bundle.release.manifest.staged_bytes
            ):
                raise UpgradeConflict("Publication artifact bytes exceed their bound")
            if (
                hashlib.sha256(content).hexdigest()
                != bundle.release.manifest.artifact_sha256s[item.key]
            ):
                raise UpgradeConflict("Publication artifact content differs")
            path = stage.path(item.key)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            artifacts[item.key] = path
        return publish_prepared_release(root, bundle.release, artifacts)


def main() -> int:
    # This helper is an explicit trusted host Docker-run entrypoint. It has no
    # service route, socket access, received path, environment-selected destination,
    # or native/runtime execution. Registration still uses the audited admin API.
    if sys.argv[1:]:
        return 2
    try:
        request = publish_stream(sys.stdin.buffer, DESTINATION)
    except (CoireError, OSError, ValueError):
        print("Prepared catalog publication failed", file=sys.stderr)
        return 2
    print(request.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
