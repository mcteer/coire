"""Cancelled core staging is removed without touching another attempt or symlinks."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageOutputRow
from coire_api.images import maintenance
from coire_api.images.maintenance import (
    purge_cancelled_transfer_staging,
    purge_stale_transfer_temporaries,
    purge_terminal_transfer_staging,
)
from coire_core.errors import ImageStorageUnavailable
from coire_core.settings import Settings

JOB = "01J00000000000000000000000"


def _attempt(root: Path, attempt: int) -> Path:
    path = root / "image-staging" / JOB / str(attempt)
    path.mkdir(parents=True)
    for directory in (root, root / "image-staging", root / "image-staging" / JOB, path):
        directory.chmod(0o700)
    return path


def _private_file(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def test_retained_output_integrity_checks_receipt_and_link_count(tmp_path: Path) -> None:
    payload = b"private png fixture"
    blob = tmp_path / "0.png"
    _private_file(blob, payload)
    row = cast(
        ImageOutputRow,
        SimpleNamespace(
            blob_key="0.png",
            size_bytes=len(payload),
            file_sha256=hashlib.sha256(payload).hexdigest(),
        ),
    )
    maintenance.verify_retained_output_blob(tmp_path, row)
    alias = tmp_path / "retained-alias"
    os.link(blob, alias)
    with pytest.raises(ImageStorageUnavailable):
        maintenance.verify_retained_output_blob(tmp_path, row)
    alias.unlink()
    blob.write_bytes(b"changed png fixture")
    with pytest.raises(ImageStorageUnavailable):
        maintenance.verify_retained_output_blob(tmp_path, row)


async def test_deleted_output_sweep_advances_past_failed_first_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC) - timedelta(hours=1)
    rows = [(uuid.UUID(int=index), now) for index in range(1, 27)]
    queries: list[str] = []
    attempted: list[uuid.UUID] = []

    class Session:
        async def execute(self, statement: object) -> SimpleNamespace:
            queries.append(str(statement))
            page = rows[:25] if len(queries) in {1, 4} else rows[25:] if len(queries) == 2 else []
            return SimpleNamespace(all=lambda: page)

        async def scalar(self, statement: object) -> datetime:
            return now

    @asynccontextmanager
    async def scope() -> AsyncIterator[Session]:
        yield Session()

    async def purge(settings: Settings, output_id: uuid.UUID) -> bool:
        attempted.append(output_id)
        if output_id != rows[-1][0]:
            raise ImageStorageUnavailable()
        return True

    monkeypatch.setattr(maintenance, "session_scope", scope)
    monkeypatch.setattr(maintenance, "purge_deleted_output", purge)
    monkeypatch.setattr(maintenance, "_deleted_after", None)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert await maintenance.sweep_deleted_outputs(settings) == 0
    assert attempted == [row[0] for row in rows[:25]]
    assert await maintenance.sweep_deleted_outputs(settings) == 1
    assert attempted[-1] == rows[-1][0]
    assert "image_outputs.deleted_at, image_outputs.id" in queries[1]
    assert await maintenance.sweep_deleted_outputs(settings) == 0
    assert attempted[-25:] == [row[0] for row in rows[:25]]


@pytest.mark.parametrize("concurrent_tombstone", [False, True])
async def test_retained_output_integrity_rechecks_missing_blob_before_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrent_tombstone: bool
) -> None:
    output_id = uuid.uuid4()
    row = cast(
        ImageOutputRow,
        SimpleNamespace(
            id=output_id,
            state="published",
            deleted_at=None,
            purged_at=None,
            blob_key="missing.png",
            size_bytes=7,
            file_sha256="a" * 64,
        ),
    )
    counts: list[int] = []
    reads = 0

    class Session:
        async def scalars(self, statement: object) -> SimpleNamespace:
            del statement
            return SimpleNamespace(all=lambda: [output_id])

        async def get(self, model: object, identity: object, **kwargs: object) -> object:
            nonlocal reads
            assert model is ImageOutputRow and identity == output_id
            reads += 1
            if reads == 2 and concurrent_tombstone:
                row.deleted_at = datetime.now(UTC)
            return row

    @asynccontextmanager
    async def scope() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(maintenance, "session_scope", scope)
    monkeypatch.setattr(
        maintenance,
        "output_integrity_failures_total",
        SimpleNamespace(add=lambda count: counts.append(count)),
    )
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent", image_blob_root=str(tmp_path)
    )
    failed, cursor = await maintenance.sweep_retained_output_integrity(settings)
    assert (failed, cursor) == (0 if concurrent_tombstone else 1, output_id)
    assert counts == ([0] if concurrent_tombstone else [0, 1])
    assert row.state == "published" and row.purged_at is None


def test_cancelled_staging_removes_only_exact_attempt(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    first = _attempt(root, 1)
    second = _attempt(root, 2)
    _private_file(first / "0.png", b"partial output")
    _private_file(first / f".1.{'a' * 32}.uploading", b"partial upload")
    _private_file(second / "0.png", b"keep")
    assert purge_cancelled_transfer_staging(root, JOB, 1)
    assert not first.exists()
    assert (second / "0.png").read_bytes() == b"keep"
    assert not purge_cancelled_transfer_staging(root, JOB, 1)


def test_terminal_staging_removes_every_known_attempt_before_releasing_hold(
    tmp_path: Path,
) -> None:
    root = tmp_path / "blobs"
    first = _attempt(root, 1)
    second = _attempt(root, 2)
    _private_file(first / "0.png", b"older partial output")
    _private_file(second / "0.png", b"latest partial output")
    assert purge_terminal_transfer_staging(root, JOB, 2)
    assert not first.exists() and not second.exists()
    assert not purge_terminal_transfer_staging(root, JOB, 2)


def test_terminal_staging_refuses_future_attempt_and_retains_all_bytes(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    first = _attempt(root, 1)
    third = _attempt(root, 3)
    _private_file(first / "0.png", b"older partial output")
    _private_file(third / "0.png", b"unexpected future attempt")
    with pytest.raises(ImageStorageUnavailable):
        purge_terminal_transfer_staging(root, JOB, 2)
    assert (first / "0.png").exists() and (third / "0.png").exists()


async def test_terminal_staging_sweep_removes_failed_job_after_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "blobs"
    attempt = _attempt(root, 1)
    earlier = attempt
    later = _attempt(root, 2)
    _private_file(attempt / "0.png", b"unpublished")
    _private_file(later / "0.png", b"unpublished later attempt")
    row = SimpleNamespace(
        id=JOB,
        state="failed",
        finished_at=datetime.now(UTC) - timedelta(hours=2),
        attempt=2,
    )

    class Scalars:
        def all(self) -> list[str]:
            return [JOB]

    class Session:
        async def scalars(self, statement: object) -> Scalars:
            return Scalars()

        async def get(self, model: object, identity: object, **kwargs: object) -> object:
            assert identity == JOB and kwargs.get("with_for_update") is True
            return row

        async def scalar(self, statement: object) -> None:
            return None

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, Session())

    monkeypatch.setattr(maintenance, "session_scope", scope)
    settings = Settings(_secrets_dir="/nonexistent", image_blob_root=str(root))  # type: ignore[call-arg]
    count, cursor = await maintenance.sweep_terminal_transfer_staging(settings)
    assert (count, cursor) == (1, JOB)
    assert not earlier.exists() and not later.exists()
    assert await maintenance.sweep_terminal_transfer_staging(settings) == (0, JOB)


async def test_orphan_staging_sweep_requires_age_and_absent_durable_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "blobs"
    stale_job = JOB
    owned_job = "01J00000000000000000000001"
    recent_job = "01J00000000000000000000002"
    for job_id in (stale_job, owned_job, recent_job):
        attempt = root / "image-staging" / job_id / "1"
        attempt.mkdir(parents=True)
        for directory in (root, root / "image-staging", attempt.parent, attempt):
            directory.chmod(0o700)
        _private_file(attempt / "0.png", b"private")
        if job_id != recent_job:
            old = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
            os.utime(attempt / "0.png", (old, old))
            os.utime(attempt, (old, old))
            os.utime(attempt.parent, (old, old))

    class Session:
        async def get(self, model: object, identity: object) -> object | None:
            return object() if identity == owned_job else None

        async def scalar(self, statement: object) -> None:
            return None

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, Session())

    monkeypatch.setattr(maintenance, "session_scope", scope)
    settings = Settings(_secrets_dir="/nonexistent", image_blob_root=str(root))  # type: ignore[call-arg]
    count, cursor = await maintenance.sweep_orphan_transfer_staging(settings)
    assert count == 1 and cursor == recent_job
    assert not (root / "image-staging" / stale_job).exists()
    assert (root / "image-staging" / owned_job / "1" / "0.png").exists()
    assert (root / "image-staging" / recent_job / "1" / "0.png").exists()


def test_cancelled_staging_refuses_symlink_and_preserves_outside_file(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    attempt = _attempt(root, 1)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep")
    (attempt / "0.png").symlink_to(outside)
    with pytest.raises(ImageStorageUnavailable):
        purge_cancelled_transfer_staging(root, JOB, 1)
    assert outside.read_bytes() == b"keep"
    assert attempt.exists()


def test_cancelled_staging_refuses_unknown_file_without_partial_purge(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    attempt = _attempt(root, 1)
    _private_file(attempt / "0.png", b"private")
    _private_file(attempt / "notes.txt", b"unexpected")
    with pytest.raises(ImageStorageUnavailable):
        purge_cancelled_transfer_staging(root, JOB, 1)
    assert (attempt / "0.png").read_bytes() == b"private"


def test_stale_temporary_sweep_keeps_active_and_final_files(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    attempt = _attempt(root, 1)
    old = attempt / f".0.{'a' * 32}.uploading"
    recent = attempt / f".1.{'b' * 32}.uploading"
    final = attempt / "0.png"
    for path in (old, recent, final):
        _private_file(path, b"private")
    now = datetime.now(UTC)
    stale = (now - timedelta(hours=2)).timestamp()
    os.utime(old, (stale, stale))
    count, cursor = purge_stale_transfer_temporaries(root, now=now)
    assert count == 1 and cursor == JOB
    assert not old.exists()
    assert recent.exists() and final.exists()
    assert purge_stale_transfer_temporaries(root, now=now, after_job=cursor)[0] == 0


def test_stale_temporary_sweep_paginates_job_directories(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    now = datetime.now(UTC)
    stale = (now - timedelta(hours=2)).timestamp()
    for index in range(30):
        job = f"01J{index:023d}"
        attempt = root / "image-staging" / job / "1"
        attempt.mkdir(parents=True)
        for directory in (root, root / "image-staging", attempt.parent, attempt):
            directory.chmod(0o700)
        temporary = attempt / f".0.{'a' * 32}.uploading"
        _private_file(temporary, b"private")
        os.utime(temporary, (stale, stale))
    first_count, cursor = purge_stale_transfer_temporaries(root, now=now)
    assert first_count == 25 and cursor is not None
    second_count, _ = purge_stale_transfer_temporaries(root, now=now, after_job=cursor)
    assert second_count == 5


def test_stale_temporary_sweep_refuses_symlink(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    attempt = _attempt(root, 1)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep")
    (attempt / f".0.{'a' * 32}.uploading").symlink_to(outside)
    with pytest.raises(ImageStorageUnavailable):
        purge_stale_transfer_temporaries(root)
    assert outside.read_bytes() == b"keep"


def test_stale_temporary_sweep_refuses_unbounded_job_inventory(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    staging = root / "image-staging"
    staging.mkdir(parents=True)
    root.chmod(0o700)
    staging.chmod(0o700)
    for index in range(4097):
        (staging / f"unknown-{index}").touch(mode=0o600)
    with pytest.raises(ImageStorageUnavailable):
        purge_stale_transfer_temporaries(root)


async def test_output_retention_is_opt_in_bounded_and_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    already_deleted = now - timedelta(minutes=1)
    rows = [
        SimpleNamespace(
            id=uuid.uuid4(),
            published_at=now - timedelta(hours=25),
            state="published",
            deleted_at=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            published_at=now - timedelta(hours=23),
            state="published",
            deleted_at=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            published_at=now - timedelta(hours=25),
            state="published",
            deleted_at=already_deleted,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            published_at=now - timedelta(hours=25),
            state="failed",
            deleted_at=None,
        ),
    ]
    queries: list[str] = []
    audits: list[dict[str, object]] = []

    class Session:
        async def scalars(self, query: object) -> object:
            queries.append(str(query))
            return SimpleNamespace(all=lambda: rows)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, Session())

    async def audit(session: object, **kwargs: object) -> None:
        del session
        audits.append(kwargs)

    monkeypatch.setattr(maintenance, "session_scope", scope)
    monkeypatch.setattr(maintenance, "write_audit", audit, raising=False)
    default = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert await maintenance.sweep_retained_outputs(default) == 0
    assert not queries and not audits
    settings = Settings(_secrets_dir="/nonexistent", image_output_retention_hours=24)  # type: ignore[call-arg]
    assert await maintenance.sweep_retained_outputs(settings) == 1
    assert rows[0].deleted_at >= now
    assert rows[1].deleted_at is None
    assert rows[2].deleted_at == already_deleted
    assert rows[3].deleted_at is None
    assert "FOR UPDATE" in queries[0] and "LIMIT" in queries[0]
    assert audits == [
        {
            "actor": "system:image-retention",
            "action": "image.output.expire",
            "target_type": "image_output",
            "target_id": str(rows[0].id),
            "detail": {"retention_hours": 24},
        }
    ]


@pytest.mark.parametrize("retention_failure", [False, True])
async def test_maintenance_starts_even_when_admission_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    retention_failure: bool,
) -> None:
    finished = asyncio.Event()
    calls: list[str] = []
    failures: list[dict[str, str]] = []
    # Real Alembic tests reconfigure logging; isolate capture from that global state.
    monkeypatch.setattr(maintenance.logger, "disabled", False)
    monkeypatch.setattr(maintenance.logger, "handlers", [caplog.handler])
    monkeypatch.setattr(maintenance.logger, "propagate", False)

    def record_failure(count: int, attributes: dict[str, str]) -> None:
        assert count == 1
        failures.append(attributes)

    monkeypatch.setattr(maintenance, "purges_total", SimpleNamespace(add=record_failure))

    def sweep(name: str) -> Callable[[Settings], Awaitable[int]]:
        async def run(settings: Settings) -> int:
            calls.append(name)
            if name == "retention" and retention_failure:
                raise RuntimeError("must-not-log-retention-detail")
            if name == "orphan_inputs":
                finished.set()
            return 0

        return run

    monkeypatch.setattr(maintenance, "sweep_retained_outputs", sweep("retention"), raising=False)
    monkeypatch.setattr(maintenance, "sweep_deleted_outputs", sweep("outputs"))

    async def quota_sweep(settings: Settings) -> maintenance.StoredQuotaReconciliation:
        calls.append("quota")
        return maintenance.StoredQuotaReconciliation(mismatched_rows=0, max_abs_drift_bytes=0)

    monkeypatch.setattr(maintenance, "sweep_stored_image_quota", quota_sweep)

    async def integrity_sweep(
        settings: Settings, *, after_output: uuid.UUID | None = None
    ) -> tuple[int, uuid.UUID]:
        calls.append("output_integrity")
        assert after_output is None
        return 0, uuid.uuid4()

    monkeypatch.setattr(maintenance, "sweep_retained_output_integrity", integrity_sweep)

    async def terminal_sweep(
        settings: Settings, *, after_job: str | None = None
    ) -> tuple[int, str]:
        calls.append("terminal_staging")
        assert after_job is None
        return 0, JOB

    monkeypatch.setattr(maintenance, "sweep_terminal_transfer_staging", terminal_sweep)

    async def orphan_sweep(settings: Settings, *, after_job: str | None = None) -> tuple[int, str]:
        calls.append("orphan_staging")
        assert after_job is None
        return 0, JOB

    monkeypatch.setattr(maintenance, "sweep_orphan_transfer_staging", orphan_sweep)

    async def temp_sweep(settings: Settings, *, after_job: str | None = None) -> tuple[int, str]:
        calls.append("transfer_temps")
        assert after_job is None
        return 0, JOB

    monkeypatch.setattr(maintenance, "sweep_stale_transfer_temporaries", temp_sweep)
    monkeypatch.setattr(maintenance, "sweep_deleted_inputs", sweep("deleted_inputs"))
    monkeypatch.setattr(maintenance, "sweep_failed_inputs", sweep("failed_inputs"))
    monkeypatch.setattr(maintenance, "sweep_orphan_inputs", sweep("orphan_inputs"))
    settings = Settings(_secrets_dir="/nonexistent", image_enabled=False)  # type: ignore[call-arg]
    worker = maintenance.ImageOutputMaintenance(settings)
    await worker.start()
    await asyncio.wait_for(finished.wait(), timeout=1)
    await worker.stop()
    assert calls == [
        "retention",
        "outputs",
        "quota",
        "output_integrity",
        "terminal_staging",
        "orphan_staging",
        "transfer_temps",
        "deleted_inputs",
        "failed_inputs",
        "orphan_inputs",
    ]
    assert failures == ([{"kind": "maintenance", "outcome": "failed"}] if retention_failure else [])
    assert "must-not-log-retention-detail" not in caplog.text
    if retention_failure:
        assert "error_type=RuntimeError" in caplog.text
