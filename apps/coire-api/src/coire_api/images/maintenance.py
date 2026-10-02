"""Crash-safe removal of private transfer staging after a fenced cancellation."""

from __future__ import annotations

import asyncio
import bisect
import errno
import logging
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from opentelemetry import trace
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import ImageInputRow, ImageJobRow, ImageOutputRow, ImageQuotaRow, session_scope
from coire_api.images.deletion import purge_output_blob
from coire_api.images.downloads import open_verified_blob
from coire_api.images.input_cleanup import (
    sweep_deleted_inputs,
    sweep_failed_inputs,
    sweep_orphan_inputs,
)
from coire_api.images.quota import _QUOTA_LOCK, _locked_rows
from coire_api.images.telemetry import (
    output_integrity_failures_total,
    purge_oldest_seconds,
    purges_total,
    quota_drift_bytes,
)
from coire_core.errors import ImageStorageUnavailable
from coire_core.models.files import ULID_PATTERN
from coire_core.settings import Settings

_TEMP = re.compile(r"\.([0-3])\.[0-9a-f]{32}\.uploading\Z")
_FINAL = frozenset(f"{index}.png" for index in range(4))
_ATTEMPT = re.compile(r"[1-9][0-9]{0,9}\Z")
_TEMP_MAX_AGE = timedelta(hours=1)
_MAX_DIRECTORY_ENTRIES = 4096
_TEMP_JOB_BATCH = 25
_BATCH_SIZE = 25
_SWEEP_SECONDS = 30.0
_QUOTA_RECONCILE_SECONDS = 300.0
_OUTPUT_INTEGRITY_SECONDS = 300.0
_deleted_after: tuple[datetime, uuid.UUID] | None = None
logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.image.maintenance")


@dataclass(frozen=True, slots=True)
class StoredQuotaReconciliation:
    """Content-free read-only comparison; drift never authorizes quota release."""

    mismatched_rows: int
    max_abs_drift_bytes: int


async def reconcile_stored_image_quota(session: AsyncSession) -> StoredQuotaReconciliation:
    """Compare owner/global stored counters with durable unpurged image and input rows."""
    await session.execute(_QUOTA_LOCK)
    expected: dict[uuid.UUID, int] = {}
    output_totals = await session.execute(
        select(ImageOutputRow.owner_user_id, func.sum(ImageOutputRow.size_bytes))
        .where(ImageOutputRow.state == "published", ImageOutputRow.purged_at.is_(None))
        .group_by(ImageOutputRow.owner_user_id)
    )
    for owner_id, amount in output_totals.all():
        expected[owner_id] = int(amount)
    input_totals = await session.execute(
        select(
            ImageInputRow.owner_user_id,
            func.sum(
                ImageInputRow.original_bytes + func.coalesce(ImageInputRow.normalized_bytes, 0)
            ),
        )
        .where(
            ImageInputRow.state.in_(("ready", "deleting")),
            ImageInputRow.held_bytes == 0,
            ImageInputRow.purged_at.is_(None),
        )
        .group_by(ImageInputRow.owner_user_id)
    )
    for owner_id, amount in input_totals.all():
        expected[owner_id] = expected.get(owner_id, 0) + int(amount)
    quotas = (await session.scalars(select(ImageQuotaRow))).all()
    differences: list[int] = []
    global_rows = [row for row in quotas if row.scope == "global"]
    if len(global_rows) != 1:
        differences.append(sum(expected.values()) + (1 if quotas else 0))
    else:
        differences.append(global_rows[0].stored_bytes - sum(expected.values()))
    for row in quotas:
        if row.scope == "owner" and row.owner_user_id is not None:
            differences.append(row.stored_bytes - expected.pop(row.owner_user_id, 0))
    differences.extend(expected.values())
    return StoredQuotaReconciliation(
        mismatched_rows=sum(value != 0 for value in differences),
        max_abs_drift_bytes=max((abs(value) for value in differences), default=0),
    )


async def sweep_stored_image_quota(settings: Settings) -> StoredQuotaReconciliation:
    """Emit bounded drift telemetry; retain all holds for operator reconciliation."""
    del settings
    with tracer.start_as_current_span("coire.api.image.quota_reconcile"):
        async with session_scope() as session:
            result = await reconcile_stored_image_quota(session)
    quota_drift_bytes.set(result.max_abs_drift_bytes)
    if result.mismatched_rows:
        logger.error(
            "image stored quota differs from durable rows mismatched_rows=%s max_abs_drift_bytes=%s",
            result.mismatched_rows,
            result.max_abs_drift_bytes,
        )
    return result


def verify_retained_output_blob(root: Path, row: ImageOutputRow) -> None:
    """Read-only receipt check; never settle quota from a missing or linked blob."""
    descriptor = open_verified_blob(root, row)
    try:
        details = os.fstat(descriptor)
        if details.st_nlink != 1 or details.st_uid != os.getuid() or details.st_mode & 0o077:
            raise ImageStorageUnavailable()
    finally:
        os.close(descriptor)


async def sweep_retained_output_integrity(
    settings: Settings, *, after_output: uuid.UUID | None = None
) -> tuple[int, uuid.UUID | None]:
    """Audit a bounded page of retained published blobs without changing ownership or quota."""
    async with session_scope() as session:
        query = (
            select(ImageOutputRow.id)
            .where(
                ImageOutputRow.state == "published",
                ImageOutputRow.deleted_at.is_(None),
                ImageOutputRow.purged_at.is_(None),
            )
            .order_by(ImageOutputRow.id)
            .limit(_BATCH_SIZE)
        )
        if after_output is not None:
            query = query.where(ImageOutputRow.id > after_output)
        ids = (await session.scalars(query)).all()
        if not ids and after_output is not None:
            ids = (
                await session.scalars(
                    select(ImageOutputRow.id)
                    .where(
                        ImageOutputRow.state == "published",
                        ImageOutputRow.deleted_at.is_(None),
                        ImageOutputRow.purged_at.is_(None),
                    )
                    .order_by(ImageOutputRow.id)
                    .limit(_BATCH_SIZE)
                )
            ).all()
    failed = 0
    root = Path(settings.image_blob_root)
    with tracer.start_as_current_span("coire.api.image.output_integrity"):
        output_integrity_failures_total.add(0)
        for output_id in ids:
            async with session_scope() as session:
                row = await session.get(ImageOutputRow, output_id)
                if (
                    row is None
                    or row.state != "published"
                    or row.deleted_at is not None
                    or row.purged_at is not None
                ):
                    continue
            try:
                await asyncio.to_thread(verify_retained_output_blob, root, row)
            except (ImageStorageUnavailable, OSError):
                # A concurrent tombstone/purge can remove the file after the first read.
                async with session_scope() as session:
                    refreshed = await session.get(ImageOutputRow, output_id)
                    if (
                        refreshed is None
                        or refreshed.deleted_at is not None
                        or refreshed.purged_at is not None
                    ):
                        continue
                failed += 1
                output_integrity_failures_total.add(1)
                logger.error("retained image output integrity failed output_id=%s", output_id)
    return failed, ids[-1] if ids else None


async def purge_deleted_output(settings: Settings, output_id: uuid.UUID) -> bool:
    """Release owner/global stored bytes only after the blob is absent on disk."""
    async with session_scope() as session:
        snapshot = await session.get(ImageOutputRow, output_id)
        if snapshot is None or snapshot.deleted_at is None or snapshot.purged_at is not None:
            return False
        global_quota, owner_quota = await _locked_rows(session, snapshot.owner_user_id)
        row = await session.get(
            ImageOutputRow, output_id, populate_existing=True, with_for_update=True
        )
        if row is None or row.deleted_at is None or row.purged_at is not None:
            return False
        await asyncio.to_thread(
            purge_output_blob,
            Path(settings.image_blob_root),
            row,
            owner_quota,
            global_quota,
        )
        return True


async def sweep_retained_outputs(settings: Settings) -> int:
    """Opt-in expiry tombstones; physical deletion and quota settlement stay separate."""
    hours = settings.image_output_retention_hours
    if hours is None:
        return 0
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=hours)
    expired = 0
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(ImageOutputRow)
                .where(
                    ImageOutputRow.state == "published",
                    ImageOutputRow.deleted_at.is_(None),
                    ImageOutputRow.published_at <= cutoff,
                )
                .order_by(ImageOutputRow.published_at, ImageOutputRow.id)
                .limit(_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in rows:
            if (
                row.state != "published"
                or row.deleted_at is not None
                or row.published_at is None
                or row.published_at > cutoff
            ):
                continue
            await write_audit(
                session,
                actor="system:image-retention",
                action="image.output.expire",
                target_type="image_output",
                target_id=str(row.id),
                detail={"retention_hours": hours},
            )
            row.deleted_at = now
            expired += 1
    if expired:
        purges_total.add(expired, {"kind": "retention", "outcome": "succeeded"})
    return expired


async def sweep_deleted_outputs(settings: Settings) -> int:
    """Advance a bounded keyset so damaged early blobs cannot starve later rows."""
    global _deleted_after
    async with session_scope() as session:
        query = (
            select(ImageOutputRow.id, ImageOutputRow.deleted_at)
            .where(ImageOutputRow.deleted_at.is_not(None), ImageOutputRow.purged_at.is_(None))
            .order_by(ImageOutputRow.deleted_at, ImageOutputRow.id)
            .limit(_BATCH_SIZE)
        )
        page = (
            await session.execute(
                query.where(tuple_(ImageOutputRow.deleted_at, ImageOutputRow.id) > _deleted_after)
                if _deleted_after is not None
                else query
            )
        ).all()
        if not page and _deleted_after is not None:
            page = (await session.execute(query)).all()
        _deleted_after = (page[-1][1], page[-1][0]) if page else None
        oldest = await session.scalar(
            select(func.min(ImageOutputRow.deleted_at)).where(
                ImageOutputRow.deleted_at.is_not(None), ImageOutputRow.purged_at.is_(None)
            )
        )
    age = max(0.0, (datetime.now(UTC) - oldest).total_seconds()) if oldest else 0.0
    purge_oldest_seconds.set(age)
    purged = 0
    for output_id, _ in page:
        try:
            if await purge_deleted_output(settings, output_id):
                purges_total.add(1, {"kind": "output", "outcome": "succeeded"})
                purged += 1
        except Exception as exc:
            purges_total.add(1, {"kind": "output", "outcome": "failed"})
            logger.error(
                "image output purge failed output_id=%s error_type=%s",
                output_id,
                type(exc).__name__,
            )
    return purged


async def sweep_terminal_transfer_staging(
    settings: Settings, *, after_job: str | None = None
) -> tuple[int, str | None]:
    """Retry private staging removal after failed/cancelled terminal commits."""
    cutoff = datetime.now(UTC) - _TEMP_MAX_AGE
    async with session_scope() as session:
        query = (
            select(ImageJobRow.id)
            .where(
                ImageJobRow.state.in_(("failed", "cancelled")),
                ImageJobRow.finished_at < cutoff,
                ImageJobRow.attempt >= 1,
            )
            .order_by(ImageJobRow.id)
            .limit(_BATCH_SIZE)
        )
        if after_job is not None:
            query = query.where(ImageJobRow.id > after_job)
        pending = (await session.scalars(query)).all()
        if not pending and after_job is not None:
            pending = (
                await session.scalars(
                    select(ImageJobRow.id)
                    .where(
                        ImageJobRow.state.in_(("failed", "cancelled")),
                        ImageJobRow.finished_at < cutoff,
                        ImageJobRow.attempt >= 1,
                    )
                    .order_by(ImageJobRow.id)
                    .limit(_BATCH_SIZE)
                )
            ).all()
    removed = 0
    for job_id in pending:
        try:
            async with session_scope() as session:
                row = await session.get(ImageJobRow, job_id, with_for_update=True)
                if (
                    row is None
                    or row.state not in {"failed", "cancelled"}
                    or row.finished_at is None
                    or row.finished_at >= cutoff
                    or row.attempt < 1
                ):
                    continue
                published = await session.scalar(
                    select(ImageOutputRow.id)
                    .where(
                        ImageOutputRow.job_id == job_id,
                        ImageOutputRow.state == "published",
                    )
                    .limit(1)
                )
                if published is not None:
                    continue
                deleted = await asyncio.to_thread(
                    purge_terminal_transfer_staging,
                    Path(settings.image_blob_root),
                    job_id,
                    row.attempt,
                )
                if deleted:
                    removed += 1
                    purges_total.add(1, {"kind": "terminal_staging", "outcome": "succeeded"})
        except Exception as exc:
            purges_total.add(1, {"kind": "terminal_staging", "outcome": "failed"})
            logger.error(
                "image terminal staging purge failed job_id=%s error_type=%s",
                job_id,
                type(exc).__name__,
            )
    return removed, pending[-1] if pending else None


def _orphan_staging_candidates(
    root: Path, *, after_job: str | None, cutoff: float
) -> tuple[tuple[str, ...], str | None]:
    """Inspect a bounded page; a candidate has no recent or untrusted filesystem entries."""
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return (), None
    try:
        details = os.fstat(root_fd)
        if details.st_uid != os.getuid() or details.st_mode & 0o077:
            raise ImageStorageUnavailable()
        staging_fd = _open_private_dir(root_fd, "image-staging")
        if staging_fd is None:
            return (), None
        try:
            names = os.listdir(staging_fd)
            if len(names) > _MAX_DIRECTORY_ENTRIES:
                raise ImageStorageUnavailable()
            jobs = sorted(name for name in names if re.fullmatch(ULID_PATTERN, name))
            start = bisect.bisect(jobs, after_job) if after_job is not None else 0
            if start >= len(jobs):
                start = 0
            page = jobs[start : start + _TEMP_JOB_BATCH]
            candidates: list[str] = []
            for job_id in page:
                job_fd = _open_private_dir(staging_fd, job_id)
                if job_fd is None:
                    continue
                try:
                    attempts = os.listdir(job_fd)
                    if not attempts or len(attempts) > _MAX_DIRECTORY_ENTRIES:
                        continue
                    stale = os.fstat(job_fd).st_mtime < cutoff
                    for attempt in attempts:
                        if _ATTEMPT.fullmatch(attempt) is None:
                            stale = False
                            break
                        attempt_fd = _open_private_dir(job_fd, attempt)
                        if attempt_fd is None:
                            stale = False
                            break
                        try:
                            files = os.listdir(attempt_fd)
                            if len(files) > _MAX_DIRECTORY_ENTRIES:
                                stale = False
                                break
                            stale = stale and os.fstat(attempt_fd).st_mtime < cutoff
                            for filename in files:
                                if filename not in _FINAL and _TEMP.fullmatch(filename) is None:
                                    stale = False
                                    break
                                file_stat = os.stat(
                                    filename, dir_fd=attempt_fd, follow_symlinks=False
                                )
                                if (
                                    not stat.S_ISREG(file_stat.st_mode)
                                    or file_stat.st_uid != os.getuid()
                                    or file_stat.st_mode & 0o077
                                    or file_stat.st_nlink != 1
                                    or file_stat.st_mtime >= cutoff
                                ):
                                    stale = False
                                    break
                        finally:
                            os.close(attempt_fd)
                        if not stale:
                            break
                    if stale:
                        candidates.append(job_id)
                finally:
                    os.close(job_fd)
            return tuple(candidates), page[-1] if page else None
        finally:
            os.close(staging_fd)
    except OSError as exc:
        raise ImageStorageUnavailable() from exc
    finally:
        os.close(root_fd)


async def sweep_orphan_transfer_staging(
    settings: Settings, *, after_job: str | None = None
) -> tuple[int, str | None]:
    """Purge old staging only when no durable job or output can own its ULID."""
    root = Path(settings.image_blob_root)
    cutoff = (datetime.now(UTC) - _TEMP_MAX_AGE).timestamp()
    candidates, cursor = await asyncio.to_thread(
        _orphan_staging_candidates, root, after_job=after_job, cutoff=cutoff
    )
    removed = 0
    for job_id in candidates:
        try:
            async with session_scope() as session:
                job = await session.get(ImageJobRow, job_id)
                output = await session.scalar(
                    select(ImageOutputRow.id).where(ImageOutputRow.job_id == job_id).limit(1)
                )
                if job is not None or output is not None:
                    continue
                attempts = await asyncio.to_thread(_terminal_staging_attempts, root, job_id, None)
                for attempt in attempts:
                    if await asyncio.to_thread(
                        purge_cancelled_transfer_staging, root, job_id, attempt
                    ):
                        removed += 1
                        purges_total.add(1, {"kind": "orphan_staging", "outcome": "succeeded"})
        except Exception as exc:
            purges_total.add(1, {"kind": "orphan_staging", "outcome": "failed"})
            logger.error(
                "image orphan staging purge failed job_id=%s error_type=%s",
                job_id,
                type(exc).__name__,
            )
    return removed, cursor


def purge_stale_transfer_temporaries(
    root: Path, *, now: datetime | None = None, after_job: str | None = None
) -> tuple[int, str | None]:
    """Visit a bounded batch of job directories and remove old temporary uploads."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ImageStorageUnavailable()
    cutoff = (current - _TEMP_MAX_AGE).timestamp()
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return 0, None
    removed = 0
    cursor: str | None = after_job
    try:
        root_stat = os.fstat(root_fd)
        if root_stat.st_uid != os.getuid() or root_stat.st_mode & 0o077:
            raise ImageStorageUnavailable()
        staging_fd = _open_private_dir(root_fd, "image-staging")
        if staging_fd is None:
            return 0, None
        try:
            entries = os.listdir(staging_fd)
            if len(entries) > _MAX_DIRECTORY_ENTRIES:
                raise ImageStorageUnavailable()
            jobs = sorted(name for name in entries if re.fullmatch(ULID_PATTERN, name) is not None)
            if not jobs:
                return 0, None
            first = bisect.bisect(jobs, after_job) if after_job is not None else 0
            if first >= len(jobs):
                first = 0
            for job_id in jobs[first : first + _TEMP_JOB_BATCH]:
                cursor = job_id
                job_fd = _open_private_dir(staging_fd, job_id)
                if job_fd is None:
                    continue
                try:
                    attempts = os.listdir(job_fd)
                    if len(attempts) > _MAX_DIRECTORY_ENTRIES:
                        raise ImageStorageUnavailable()
                    for attempt in attempts:
                        if _ATTEMPT.fullmatch(attempt) is None:
                            continue
                        attempt_fd = _open_private_dir(job_fd, attempt)
                        if attempt_fd is None:
                            continue
                        try:
                            names = os.listdir(attempt_fd)
                            if len(names) > _MAX_DIRECTORY_ENTRIES:
                                raise ImageStorageUnavailable()
                            removed_here = 0
                            for name in names:
                                if _TEMP.fullmatch(name) is None:
                                    continue
                                details = os.stat(name, dir_fd=attempt_fd, follow_symlinks=False)
                                if (
                                    not stat.S_ISREG(details.st_mode)
                                    or details.st_uid != os.getuid()
                                    or details.st_mode & 0o077
                                    or details.st_nlink != 1
                                ):
                                    raise ImageStorageUnavailable()
                                if details.st_mtime < cutoff:
                                    os.unlink(name, dir_fd=attempt_fd)
                                    removed += 1
                                    removed_here += 1
                            if removed_here:
                                os.fsync(attempt_fd)
                        finally:
                            os.close(attempt_fd)
                finally:
                    os.close(job_fd)
        finally:
            os.close(staging_fd)
    except OSError as exc:
        raise ImageStorageUnavailable() from exc
    finally:
        os.close(root_fd)
    return removed, cursor


async def sweep_stale_transfer_temporaries(
    settings: Settings, *, after_job: str | None = None
) -> tuple[int, str | None]:
    """Run the bounded filesystem sweep outside the API event loop."""
    with tracer.start_as_current_span("coire.api.image.transfer_temp_sweep"):
        try:
            count, cursor = await asyncio.to_thread(
                purge_stale_transfer_temporaries,
                Path(settings.image_blob_root),
                after_job=after_job,
            )
        except Exception:
            purges_total.add(1, {"kind": "transfer_temp", "outcome": "failed"})
            raise
        if count:
            purges_total.add(count, {"kind": "transfer_temp", "outcome": "succeeded"})
        return count, cursor


class ImageOutputMaintenance:
    """API-owned bounded private blob cleanup, including inputs after disablement."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._transfer_cursor: str | None = None
        self._terminal_cursor: str | None = None
        self._orphan_cursor: str | None = None
        self._integrity_cursor: uuid.UUID | None = None
        self._last_integrity_at: float | None = None
        self._last_quota_reconcile_at: float | None = None

    async def _sweep_stored_quota(self, settings: Settings) -> StoredQuotaReconciliation | None:
        now = time.monotonic()
        if (
            self._last_quota_reconcile_at is not None
            and now - self._last_quota_reconcile_at < _QUOTA_RECONCILE_SECONDS
        ):
            return None
        result = await sweep_stored_image_quota(settings)
        self._last_quota_reconcile_at = now
        return result

    async def _sweep_output_integrity(self, settings: Settings) -> int:
        now = time.monotonic()
        if (
            self._last_integrity_at is not None
            and now - self._last_integrity_at < _OUTPUT_INTEGRITY_SECONDS
        ):
            return 0
        failed, self._integrity_cursor = await sweep_retained_output_integrity(
            settings, after_output=self._integrity_cursor
        )
        self._last_integrity_at = now
        return failed

    async def _sweep_terminal_staging(self, settings: Settings) -> int:
        count, self._terminal_cursor = await sweep_terminal_transfer_staging(
            settings, after_job=self._terminal_cursor
        )
        return count

    async def _sweep_transfer_temporaries(self, settings: Settings) -> int:
        count, self._transfer_cursor = await sweep_stale_transfer_temporaries(
            settings, after_job=self._transfer_cursor
        )
        return count

    async def _sweep_orphan_staging(self, settings: Settings) -> int:
        count, self._orphan_cursor = await sweep_orphan_transfer_staging(
            settings, after_job=self._orphan_cursor
        )
        return count

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="image-output-maintenance")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            for sweep in (
                sweep_retained_outputs,
                sweep_deleted_outputs,
                self._sweep_stored_quota,
                self._sweep_output_integrity,
                self._sweep_terminal_staging,
                self._sweep_orphan_staging,
                self._sweep_transfer_temporaries,
                sweep_deleted_inputs,
                sweep_failed_inputs,
                sweep_orphan_inputs,
            ):
                try:
                    with tracer.start_as_current_span("coire.api.image.maintenance") as span:
                        span.set_attribute("coire.image.sweep", sweep.__name__)
                        await sweep(self.settings)
                except Exception as exc:
                    purges_total.add(1, {"kind": "maintenance", "outcome": "failed"})
                    logger.error(
                        "image maintenance pass failed sweep=%s error_type=%s",
                        sweep.__name__,
                        type(exc).__name__,
                    )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_SWEEP_SECONDS)
            except TimeoutError:
                continue


def _open_private_dir(parent: int, name: str) -> int | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        return None
    details = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        os.close(descriptor)
        raise ImageStorageUnavailable()
    return descriptor


def _terminal_staging_attempts(
    root: Path, job_id: str, latest_attempt: int | None
) -> tuple[int, ...]:
    """Inspect only generated attempt directories under a private job key."""
    if re.fullmatch(ULID_PATTERN, job_id) is None or (
        latest_attempt is not None and latest_attempt < 1
    ):
        raise ImageStorageUnavailable()
    descriptors: list[int] = []
    try:
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return ()
        descriptors.append(root_fd)
        root_info = os.fstat(root_fd)
        if root_info.st_uid != os.getuid() or root_info.st_mode & 0o077:
            raise ImageStorageUnavailable()
        for part in ("image-staging", job_id):
            opened = _open_private_dir(descriptors[-1], part)
            if opened is None:
                return ()
            descriptors.append(opened)
        names = os.listdir(descriptors[-1])
        if len(names) > _MAX_DIRECTORY_ENTRIES:
            raise ImageStorageUnavailable()
        attempts: list[int] = []
        for name in names:
            if _ATTEMPT.fullmatch(name) is None:
                raise ImageStorageUnavailable()
            attempt = int(name)
            if latest_attempt is not None and attempt > latest_attempt:
                raise ImageStorageUnavailable()
            attempts.append(attempt)
        return tuple(sorted(attempts))
    except OSError as exc:
        raise ImageStorageUnavailable() from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def purge_terminal_transfer_staging(root: Path, job_id: str, latest_attempt: int) -> bool:
    """Remove all known attempts before terminal quota release; refuse a future attempt."""
    attempts = _terminal_staging_attempts(root, job_id, latest_attempt)
    removed = False
    for attempt in attempts:
        removed = purge_cancelled_transfer_staging(root, job_id, attempt) or removed
    return removed


def purge_cancelled_transfer_staging(root: Path, job_id: str, attempt: int) -> bool:
    """Unlink only this attempt's private files after the job row excludes new uploads."""
    if re.fullmatch(ULID_PATTERN, job_id) is None or attempt < 1:
        raise ImageStorageUnavailable()
    descriptors: list[int] = []
    try:
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        descriptors.append(root_fd)
        if (
            not stat.S_ISDIR(os.fstat(root_fd).st_mode)
            or os.fstat(root_fd).st_uid != os.getuid()
            or os.fstat(root_fd).st_mode & 0o077
        ):
            raise ImageStorageUnavailable()
        for part in ("image-staging", job_id, str(attempt)):
            opened = _open_private_dir(descriptors[-1], part)
            if opened is None:
                return False
            descriptors.append(opened)
        attempt_fd = descriptors[-1]
        names = os.listdir(attempt_fd)
        if len(names) > 4096 or any(
            name not in _FINAL and _TEMP.fullmatch(name) is None for name in names
        ):
            raise ImageStorageUnavailable()
        for name in names:
            details = os.stat(name, dir_fd=attempt_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_mode & 0o077
                or details.st_nlink != 1
            ):
                raise ImageStorageUnavailable()
        for name in names:
            os.unlink(name, dir_fd=attempt_fd)
        os.fsync(attempt_fd)
        os.rmdir(str(attempt), dir_fd=descriptors[-2])
        os.fsync(descriptors[-2])
        try:
            os.rmdir(job_id, dir_fd=descriptors[-3])
            os.fsync(descriptors[-3])
        except OSError as exc:
            if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                raise
        return True
    except OSError as exc:
        raise ImageStorageUnavailable() from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
