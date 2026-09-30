"""Quota holds fail closed and move bytes only under owner/global row locks."""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageQuotaRow
from coire_api.images import quota
from coire_core.errors import ImageConflict, ImageQuotaExceeded, ImageStorageUnavailable
from coire_core.settings import Settings

OWNER = uuid.uuid4()


class FakeSession:
    def __init__(self) -> None:
        self.global_row: ImageQuotaRow | None = None
        self.owner_row: ImageQuotaRow | None = None
        self.calls: list[str] = []

    async def execute(self, statement: object) -> None:
        self.calls.append("advisory")

    async def scalar(self, statement: object) -> ImageQuotaRow | None:
        kind = "global" if self.calls[-1] in {"advisory", "owner"} else "owner"
        self.calls.append(kind)
        return self.global_row if kind == "global" else self.owner_row

    def add(self, row: ImageQuotaRow) -> None:
        if row.scope == "global":
            self.global_row = row
        else:
            self.owner_row = row

    async def flush(self) -> None:
        pass


def _settings(*, owner_cap: int = 20, global_cap: int = 30) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        image_owner_storage_quota_bytes=owner_cap,
        image_global_storage_quota_bytes=global_cap,
    )


async def test_first_use_reserves_owner_global_and_exact_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    settings = _settings(owner_cap=8, global_cap=8)
    monkeypatch.setattr(quota, "_free_bytes", lambda _: settings.image_disk_safety_floor_bytes + 8)
    await quota.reserve_storage_hold(cast(AsyncSession, session), OWNER, 8, settings)
    assert session.calls == ["advisory", "global", "owner"]
    assert session.global_row is not None and session.owner_row is not None
    assert session.global_row.held_bytes == session.owner_row.held_bytes == 8
    assert session.owner_row.owner_user_id == OWNER
    with pytest.raises(ImageQuotaExceeded):
        await quota.reserve_storage_hold(cast(AsyncSession, session), OWNER, 1, settings)
    assert session.global_row.held_bytes == session.owner_row.held_bytes == 8


async def test_global_cap_and_disk_floor_fail_without_mutating_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    settings = _settings(owner_cap=20, global_cap=8)
    monkeypatch.setattr(quota, "_free_bytes", lambda _: settings.image_disk_safety_floor_bytes + 7)
    with pytest.raises(ImageStorageUnavailable):
        await quota.reserve_storage_hold(cast(AsyncSession, session), OWNER, 8, settings)
    assert session.global_row is not None and session.global_row.held_bytes == 0
    monkeypatch.setattr(quota, "_free_bytes", lambda _: settings.image_disk_safety_floor_bytes + 30)
    await quota.reserve_storage_hold(cast(AsyncSession, session), OWNER, 8, settings)
    with pytest.raises(ImageQuotaExceeded):
        await quota.reserve_storage_hold(cast(AsyncSession, session), OWNER, 1, settings)


async def test_settle_and_release_require_exact_owner_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    settings = _settings()
    monkeypatch.setattr(quota, "_free_bytes", lambda _: settings.image_disk_safety_floor_bytes + 30)
    await quota.reserve_storage_hold(cast(AsyncSession, session), OWNER, 10, settings)
    with pytest.raises(ImageConflict):
        await quota.settle_storage_hold(cast(AsyncSession, session), OWNER, 11, 10)
    with pytest.raises(ImageConflict):
        await quota.settle_storage_hold(cast(AsyncSession, session), uuid.uuid4(), 10, 10)
    await quota.settle_storage_hold(cast(AsyncSession, session), OWNER, 10, 7)
    assert session.owner_row is not None and session.global_row is not None
    assert session.owner_row.held_bytes == session.global_row.held_bytes == 0
    assert session.owner_row.stored_bytes == session.global_row.stored_bytes == 7
    with pytest.raises(ImageConflict):
        await quota.release_storage_hold(cast(AsyncSession, session), OWNER, 10)


async def test_release_failed_hold_is_once_only(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    settings = _settings()
    monkeypatch.setattr(quota, "_free_bytes", lambda _: settings.image_disk_safety_floor_bytes + 30)
    await quota.reserve_storage_hold(cast(AsyncSession, session), OWNER, 9, settings)
    await quota.release_storage_hold(cast(AsyncSession, session), OWNER, 9)
    assert session.owner_row is not None and session.owner_row.held_bytes == 0
    with pytest.raises(ImageConflict):
        await quota.release_storage_hold(cast(AsyncSession, session), OWNER, 9)


async def test_disk_floor_counts_existing_unwritten_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    settings = _settings(owner_cap=20, global_cap=20)
    monkeypatch.setattr(quota, "_free_bytes", lambda _: settings.image_disk_safety_floor_bytes + 8)
    await quota.reserve_storage_hold(cast(AsyncSession, session), OWNER, 5, settings)
    with pytest.raises(ImageStorageUnavailable):
        await quota.reserve_storage_hold(cast(AsyncSession, session), OWNER, 4, settings)
    assert session.global_row is not None and session.global_row.held_bytes == 5
