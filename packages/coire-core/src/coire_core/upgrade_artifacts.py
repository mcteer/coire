"""Verify operator-prepared artifacts without accepting client filesystem paths."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import uuid4

from opentelemetry import metrics, trace
from pydantic import JsonValue

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    COHORT_ORDER,
    UpgradeComposeMigration,
    UpgradeKind,
    UpgradeRelease,
    UpgradeReleaseRegister,
)
from coire_core.upgrade_journal import PrivateUpgradeStore

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.deploy.upgrades")
preparations = metrics.get_meter("coire.deploy.upgrades").create_counter(
    "coire_upgrade_artifact_preparations_total",
    description="Verified immutable artifact publications",
)


def verify_prepared_release(root: Path, request: UpgradeReleaseRegister) -> UpgradeRelease:
    """Every artifact is a private regular file beneath one validated artifact key."""
    catalog = PrivateUpgradeStore(root)
    directory = catalog.path(request.prepared_artifact_id)
    if not directory.is_dir() or directory.is_symlink():
        raise UpgradeUnavailable("Prepared release is unavailable")
    store = PrivateUpgradeStore(directory)
    release = store.read("release.json", UpgradeRelease)
    if release is None:
        raise UpgradeUnavailable("Prepared release manifest is absent")
    if (
        release.prepared_artifact_id != request.prepared_artifact_id
        or release.manifest_sha256 != request.expected_sha256
    ):
        raise UpgradeConflict("Prepared release identity differs")
    _verify_contents(store, release)
    return release


def _verify_contents(store: PrivateUpgradeStore, release: UpgradeRelease) -> None:
    total = 0
    for key, expected in release.manifest.artifact_sha256s.items():
        path = store.path(key)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise UpgradeUnavailable("Prepared artifact cannot be read safely") from None
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_mode & 0o077
                or before.st_nlink != 1
            ):
                raise UpgradeUnavailable("Prepared artifact ownership differs")
            if before.st_size > release.manifest.staged_bytes - total:
                raise UpgradeConflict("Prepared artifact exceeds declared byte count")
            digest = hashlib.sha256()
            read_bytes = 0
            with os.fdopen(fd, "rb", closefd=False) as handle:
                while chunk := handle.read(1024 * 1024):
                    read_bytes += len(chunk)
                    if read_bytes > before.st_size:
                        raise UpgradeUnavailable("Prepared artifact grew during verification")
                    digest.update(chunk)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise UpgradeUnavailable("Prepared artifact changed during verification")
            observed = path.lstat()
            if (observed.st_dev, observed.st_ino) != (before.st_dev, before.st_ino):
                raise UpgradeUnavailable("Prepared artifact identity changed during verification")
            if digest.hexdigest() != expected:
                raise UpgradeConflict("Prepared artifact content differs")
            total += before.st_size
        finally:
            os.close(fd)
    if total != release.manifest.staged_bytes:
        raise UpgradeConflict("Prepared release byte count differs")
    if release.manifest.kind is UpgradeKind.CONTROL_PLANE:
        _verify_frozen_compose(store, release)


def _verify_frozen_compose(
    store: PrivateUpgradeStore, release: UpgradeRelease
) -> dict[str, JsonValue]:
    expected = release.manifest.compose_sha256
    if expected is None or release.manifest.artifact_sha256s.get("compose.json") != expected:
        raise UpgradeConflict("Prepared Compose identity differs")
    fd = os.open(store.path("compose.json"), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, "rb", closefd=False) as reader:
            payload = reader.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024 or hashlib.sha256(payload).hexdigest() != expected:
            raise UpgradeConflict("Prepared Compose identity differs")

        def unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate field")
                result[key] = value
            return result

        try:
            document = json.loads(payload, object_pairs_hook=unique_fields)
        except ValueError:
            raise UpgradeUnavailable("Prepared Compose document is corrupt") from None
        if (
            not isinstance(document, dict)
            or document.get("name") != release.manifest.project
            or not isinstance(document.get("services"), dict)
        ):
            raise UpgradeConflict("Prepared Compose project differs")
        services = document["services"]
        if "coire-migrate" in services:
            try:
                UpgradeComposeMigration.model_validate(services["coire-migrate"])
            except ValueError:
                raise UpgradeConflict("Prepared migration configuration is unsupported") from None
        if {item.service for item in release.manifest.services} != set(COHORT_ORDER).intersection(
            services
        ):
            raise UpgradeConflict("Prepared Compose cohort differs")
        for item in release.manifest.services:
            actual = services.get(item.service)
            if (
                actual != item.configuration
                or not isinstance(actual, dict)
                or actual.get("image") != item.image_id
            ):
                raise UpgradeConflict("Prepared Compose service configuration differs")
        return cast(dict[str, JsonValue], document)
    finally:
        os.close(fd)


def read_prepared_compose(root: Path, release: UpgradeRelease) -> dict[str, JsonValue]:
    """Read only the accepted immutable catalog identity, then recheck the exact Compose bytes."""
    if release.manifest.kind is not UpgradeKind.CONTROL_PLANE:
        raise UpgradeConflict("Prepared Compose requires a Core release")
    current = verify_prepared_release(
        root,
        UpgradeReleaseRegister(
            prepared_artifact_id=release.prepared_artifact_id,
            expected_sha256=release.manifest_sha256,
        ),
    )
    if current != release:
        raise UpgradeConflict("Prepared catalog release differs from the accepted release")
    catalog = PrivateUpgradeStore(root)
    return _verify_frozen_compose(
        PrivateUpgradeStore(catalog.path(release.prepared_artifact_id)), release
    )


def _publish_prepared_release(
    root: Path,
    release: UpgradeRelease,
    artifacts: Mapping[str, Path],
) -> UpgradeReleaseRegister:
    """Host-only publication of verified operator artifacts into the private catalog.

    API registration only reads this catalog; it never accepts artifact paths.
    Failed preparation removes only the unpublished temporary directory.
    """
    catalog = PrivateUpgradeStore(root)
    destination = catalog.path(release.prepared_artifact_id)
    if set(artifacts) != set(release.manifest.artifact_sha256s):
        raise UpgradeConflict("Prepared artifact set differs from manifest")
    with catalog.ownership("publish.lock"):
        if destination.exists() or destination.is_symlink():
            raise UpgradeConflict("Prepared release already exists")
        temporary = catalog.path("prepare-" + uuid4().hex + ".pending")
        staged = PrivateUpgradeStore(temporary)
        try:
            for key, source in artifacts.items():
                fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    before = os.fstat(fd)
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_uid != os.getuid()
                        or before.st_nlink != 1
                        or before.st_size > release.manifest.staged_bytes
                    ):
                        raise UpgradeUnavailable("Operator artifact cannot be trusted")
                    output = os.open(
                        staged.path(key),
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                    )
                    with os.fdopen(output, "wb") as writer:
                        with os.fdopen(fd, "rb", closefd=False) as reader:
                            copied = 0
                            while chunk := reader.read(1024 * 1024):
                                copied += len(chunk)
                                if copied > before.st_size:
                                    raise UpgradeUnavailable(
                                        "Operator artifact grew during preparation"
                                    )
                                writer.write(chunk)
                        writer.flush()
                        os.fsync(writer.fileno())
                    after = os.fstat(fd)
                    observed = source.lstat()
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
                    ) or (observed.st_dev, observed.st_ino) != (before.st_dev, before.st_ino):
                        raise UpgradeUnavailable("Operator artifact changed during preparation")
                finally:
                    os.close(fd)
            staged.write("release.json", release, replace=False)
            _verify_contents(staged, release)
            staged.sync()
            os.rename(temporary, destination)
            catalog.sync()
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return UpgradeReleaseRegister(
        prepared_artifact_id=release.prepared_artifact_id,
        expected_sha256=release.manifest_sha256,
    )


def publish_prepared_release(
    root: Path,
    release: UpgradeRelease,
    artifacts: Mapping[str, Path],
) -> UpgradeReleaseRegister:
    with tracer.start_as_current_span(
        "coire.deploy.upgrade_prepare",
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        span.set_attribute("coire.release_id", str(release.release_id))
        span.set_attribute("coire.user_id", str(release.prepared_by))
        span.set_attribute("coire.upgrade_kind", release.manifest.kind.value)
        try:
            result = _publish_prepared_release(root, release, artifacts)
        except (CoireError, OSError) as error:
            preparations.add(1, {"kind": release.manifest.kind.value, "outcome": "failed"})
            logger.info(
                "upgrade preparation refused",
                extra={
                    "user_id": str(release.prepared_by),
                    "release_id": str(release.release_id),
                    "safe_reason": type(error).__name__,
                },
            )
            if isinstance(error, OSError):
                raise UpgradeUnavailable("Operator artifact preparation unavailable") from None
            raise
        preparations.add(1, {"kind": release.manifest.kind.value, "outcome": "published"})
        logger.info(
            "upgrade release prepared",
            extra={
                "user_id": str(release.prepared_by),
                "release_id": str(release.release_id),
                "manifest_sha256": release.manifest_sha256,
            },
        )
        return result
