"""Queue, day and disk holds for a future durable image admission transaction."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageQuotaRow
from coire_api.images import job_capacity
from coire_core.errors import ImageConflict, ImageQuotaExceeded, ImageStorageUnavailable
from coire_core.settings import Settings

OWNER = uuid.uuid4()
NOW = datetime(2026, 9, 30, tzinfo=UTC)
SESSION = cast(AsyncSession, SimpleNamespace())


def _rows() -> tuple[ImageQuotaRow, ImageQuotaRow]:
    owner = cast(
        ImageQuotaRow,
        SimpleNamespace(
            scope="owner",
            owner_user_id=OWNER,
            held_bytes=0,
            stored_bytes=0,
            pending_jobs=0,
            day_bucket=NOW.date(),
            held_outputs=0,
            consumed_outputs=0,
        ),
    )
    global_row = cast(
        ImageQuotaRow,
        SimpleNamespace(
            scope="global",
            owner_user_id=None,
            held_bytes=0,
            stored_bytes=0,
            pending_jobs=0,
            day_bucket=NOW.date(),
            held_outputs=0,
            consumed_outputs=0,
        ),
    )
    return global_row, owner


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        image_output_max_bytes=8,
        image_owner_storage_quota_bytes=32,
        image_global_storage_quota_bytes=64,
        image_disk_safety_floor_bytes=2 * 1024**3,
    )


def _install(monkeypatch: pytest.MonkeyPatch, rows: tuple[ImageQuotaRow, ImageQuotaRow]) -> None:
    async def locked(session: object, owner_id: uuid.UUID) -> tuple[ImageQuotaRow, ImageQuotaRow]:
        assert owner_id == OWNER
        return rows

    monkeypatch.setattr(job_capacity, "_locked_rows", locked)
    monkeypatch.setattr(job_capacity, "_free_bytes", lambda root: 2 * 1024**3 + 64)


def test_reserve_then_start_consumes_day_but_keeps_byte_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _rows()
    _install(monkeypatch, rows)
    amount = asyncio.run(
        job_capacity.reserve_image_job_capacity(SESSION, OWNER, 2, _settings(), now=NOW)
    )
    assert amount == 16
    assert all(row.pending_jobs == 1 and row.held_bytes == 16 for row in rows)
    assert all(row.held_outputs == 2 and row.consumed_outputs == 0 for row in rows)
    asyncio.run(job_capacity.mark_image_job_started(SESSION, OWNER, 2))
    assert all(row.pending_jobs == 0 and row.held_bytes == 16 for row in rows)
    assert all(row.held_outputs == 0 and row.consumed_outputs == 2 for row in rows)
    with pytest.raises(ImageConflict):
        asyncio.run(job_capacity.mark_image_job_started(SESSION, OWNER, 2))


def test_pending_cancellation_releases_all_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _rows()
    _install(monkeypatch, rows)
    asyncio.run(job_capacity.reserve_image_job_capacity(SESSION, OWNER, 2, _settings(), now=NOW))
    asyncio.run(job_capacity.release_pending_image_job_capacity(SESSION, OWNER, 2, 16))
    assert all(row.pending_jobs == row.held_bytes == row.held_outputs == 0 for row in rows)


@pytest.mark.parametrize(
    "owner_pending,global_pending,day_used", [(4, 0, 0), (0, 32, 0), (0, 0, 100)]
)
def test_limits_refuse_without_mutating_counters(
    monkeypatch: pytest.MonkeyPatch, owner_pending: int, global_pending: int, day_used: int
) -> None:
    global_row, owner = rows = _rows()
    owner.pending_jobs = owner_pending
    global_row.pending_jobs = global_pending
    owner.consumed_outputs = day_used
    _install(monkeypatch, rows)
    with pytest.raises(ImageQuotaExceeded):
        asyncio.run(
            job_capacity.reserve_image_job_capacity(SESSION, OWNER, 1, _settings(), now=NOW)
        )
    assert owner.pending_jobs == owner_pending and global_row.pending_jobs == global_pending
    assert owner.held_bytes == global_row.held_bytes == 0


def test_utc_rollover_retains_pending_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    global_row, owner = rows = _rows()
    owner.day_bucket = global_row.day_bucket = date(2026, 9, 29)
    owner.held_outputs = global_row.held_outputs = 2
    owner.consumed_outputs = global_row.consumed_outputs = 100
    _install(monkeypatch, rows)
    asyncio.run(job_capacity.reserve_image_job_capacity(SESSION, OWNER, 1, _settings(), now=NOW))
    assert owner.day_bucket == NOW.date() and owner.consumed_outputs == 0
    assert owner.held_outputs == 3


def test_start_after_midnight_counts_against_new_day(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _rows()
    _install(monkeypatch, rows)
    asyncio.run(
        job_capacity.reserve_image_job_capacity(
            SESSION, OWNER, 1, _settings(), now=NOW - timedelta(days=1)
        )
    )
    rows[1].consumed_outputs = 98
    asyncio.run(job_capacity.mark_image_job_started(SESSION, OWNER, 1, now=NOW))
    assert rows[1].day_bucket == NOW.date()
    assert rows[1].consumed_outputs == 1


def test_disk_floor_and_storage_quota_fail_before_counters_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _rows()
    _install(monkeypatch, rows)
    monkeypatch.setattr(job_capacity, "_free_bytes", lambda root: 2 * 1024**3 + 7)
    with pytest.raises(ImageStorageUnavailable):
        asyncio.run(
            job_capacity.reserve_image_job_capacity(SESSION, OWNER, 1, _settings(), now=NOW)
        )
    assert rows[0].held_bytes == rows[1].held_bytes == 0
    monkeypatch.setattr(job_capacity, "_free_bytes", lambda root: 2 * 1024**3 + 64)
    rows[1].stored_bytes = 32
    with pytest.raises(ImageQuotaExceeded):
        asyncio.run(
            job_capacity.reserve_image_job_capacity(SESSION, OWNER, 1, _settings(), now=NOW)
        )
    assert rows[0].held_bytes == rows[1].held_bytes == 0


def test_failed_reservation_does_not_roll_day_or_consume_allowance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _rows()
    rows[1].day_bucket = date(2026, 9, 29)
    rows[1].consumed_outputs = 100
    _install(monkeypatch, rows)
    monkeypatch.setattr(job_capacity, "_free_bytes", lambda root: 0)
    with pytest.raises(ImageStorageUnavailable):
        asyncio.run(
            job_capacity.reserve_image_job_capacity(SESSION, OWNER, 1, _settings(), now=NOW)
        )
    assert rows[1].day_bucket == date(2026, 9, 29)
    assert rows[1].consumed_outputs == 100
