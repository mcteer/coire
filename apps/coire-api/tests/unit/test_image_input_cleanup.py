"""Failed recipe bytes and orphan originals are removed before quota is credited."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from coire_api.db import ImageInputRow, ImageQuotaRow
from coire_api.images import input_cleanup
from coire_core.errors import ImageStorageUnavailable
from coire_core.settings import Settings

OWNER = uuid.uuid4()


def _row() -> ImageInputRow:
    input_id = uuid.uuid4()
    return ImageInputRow(
        id=input_id,
        owner_user_id=OWNER,
        purpose="recipe",
        original_key=str(input_id),
        original_bytes=7,
        original_sha256="a" * 64,
        state="failed",
        held_bytes=7,
        active_references=0,
        created_at=datetime.now(UTC),
    )


def _quotas() -> tuple[ImageQuotaRow, ImageQuotaRow]:
    owner = cast(
        ImageQuotaRow,
        SimpleNamespace(scope="owner", owner_user_id=OWNER, held_bytes=7, stored_bytes=0),
    )
    global_row = cast(ImageQuotaRow, SimpleNamespace(scope="global", held_bytes=7, stored_bytes=0))
    return owner, global_row


@pytest.mark.parametrize("held", [0, 7])
def test_deleted_input_purge_releases_only_after_unlink(tmp_path: Path, held: int) -> None:
    row = _row()
    row.state = "deleting"
    row.deleted_at = datetime.now(UTC)
    row.held_bytes = held
    path = tmp_path / str(row.id)
    path.write_bytes(b"pngdata")
    owner, global_row = _quotas()
    owner.held_bytes = global_row.held_bytes = held
    owner.stored_bytes = global_row.stored_bytes = 7 - held
    input_cleanup.purge_deleted_input_bytes(tmp_path, row, owner, global_row)
    assert not path.exists() and row.state == "purged" and row.purged_at is not None
    assert row.held_bytes == owner.held_bytes == global_row.held_bytes == 0
    assert owner.stored_bytes == global_row.stored_bytes == 0
    input_cleanup.purge_deleted_input_bytes(tmp_path, row, owner, global_row)


def test_deleted_input_purge_rejects_symlink_and_references(tmp_path: Path) -> None:
    row = _row()
    row.state = "deleting"
    row.deleted_at = datetime.now(UTC)
    owner, global_row = _quotas()
    target = tmp_path / str(row.id)
    target.symlink_to(tmp_path / "outside")
    with pytest.raises(ImageStorageUnavailable):
        input_cleanup.purge_deleted_input_bytes(tmp_path, row, owner, global_row)
    assert row.purged_at is None and owner.held_bytes == 7
    target.unlink()
    row.active_references = 1
    with pytest.raises(ImageStorageUnavailable):
        input_cleanup.purge_deleted_input_bytes(tmp_path, row, owner, global_row)
    assert row.purged_at is None and owner.held_bytes == 7


def test_deleted_input_purge_retains_quota_for_hardlinked_original(tmp_path: Path) -> None:
    row = _row()
    row.state = "deleting"
    row.deleted_at = datetime.now(UTC)
    original = tmp_path / str(row.id)
    original.write_bytes(b"pngdata")
    alias = tmp_path / "retained-alias"
    os.link(original, alias)
    owner, global_row = _quotas()
    with pytest.raises(ImageStorageUnavailable):
        input_cleanup.purge_deleted_input_bytes(tmp_path, row, owner, global_row)
    assert original.exists() and alias.exists()
    assert row.purged_at is None and owner.held_bytes == global_row.held_bytes == 7


def test_failed_normalized_input_retains_hold_for_hardlinked_derived(tmp_path: Path) -> None:
    original = tmp_path / "original"
    derived = tmp_path / "derived"
    original.mkdir()
    derived.mkdir()
    row = _row()
    row.purpose = "init"
    row.held_bytes = 7 + 10 * 1024 * 1024
    (original / str(row.id)).write_bytes(b"pngdata")
    normalized = derived / str(row.id)
    normalized.write_bytes(b"normalized")
    alias = tmp_path / "retained-alias"
    os.link(normalized, alias)
    owner, global_row = _quotas()
    owner.held_bytes = global_row.held_bytes = row.held_bytes
    with pytest.raises(ImageStorageUnavailable):
        input_cleanup.purge_failed_input_bytes(original, row, owner, global_row, derived)
    assert normalized.exists() and alias.exists()
    assert row.purged_at is None and owner.held_bytes == global_row.held_bytes == row.held_bytes


def test_deleted_input_missing_file_is_retry_safe(tmp_path: Path) -> None:
    row = _row()
    row.state = "deleting"
    row.deleted_at = datetime.now(UTC)
    owner, global_row = _quotas()
    input_cleanup.purge_deleted_input_bytes(tmp_path, row, owner, global_row)
    assert row.state == "purged" and row.purged_at is not None
    assert owner.held_bytes == global_row.held_bytes == 0


def test_deleted_input_maintenance_locks_quota_before_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _row()
    row.state = "deleting"
    row.deleted_at = datetime.now(UTC)
    owner, global_row = _quotas()
    calls: list[str] = []

    class FakeSession:
        async def get(self, model: type[object], identity: object, **kwargs: object) -> object:
            assert model is ImageInputRow and identity == row.id
            calls.append("row_lock" if kwargs.get("with_for_update") else "snapshot")
            return row

    @asynccontextmanager
    async def scope() -> AsyncIterator[FakeSession]:
        yield FakeSession()

    async def locked(session: object, owner_id: uuid.UUID) -> tuple[ImageQuotaRow, ImageQuotaRow]:
        assert owner_id == OWNER
        calls.append("quotas")
        return global_row, owner

    monkeypatch.setattr(input_cleanup, "session_scope", scope)
    monkeypatch.setattr(input_cleanup, "_locked_rows", locked)
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent", image_input_original_root=str(tmp_path)
    )
    assert asyncio.run(input_cleanup.purge_deleted_input(settings, row.id))
    assert calls == ["snapshot", "quotas", "row_lock"]
    assert row.state == "purged" and owner.held_bytes == global_row.held_bytes == 0


def test_failed_input_purge_is_ordered_idempotent_and_missing_safe(tmp_path: Path) -> None:
    row = _row()
    path = tmp_path / str(row.id)
    path.write_bytes(b"pngdata")
    owner, global_row = _quotas()
    input_cleanup.purge_failed_input_bytes(tmp_path, row, owner, global_row)
    assert not path.exists() and row.purged_at is not None
    assert row.state == "failed" and row.held_bytes == 0
    assert owner.held_bytes == global_row.held_bytes == 0
    input_cleanup.purge_failed_input_bytes(tmp_path, row, owner, global_row)
    assert owner.held_bytes == 0

    row2 = _row()
    owner2, global2 = _quotas()
    input_cleanup.purge_failed_input_bytes(tmp_path, row2, owner2, global2)
    assert row2.purged_at is not None and owner2.held_bytes == 0


def test_failed_input_purge_rejects_symlink_or_wrong_quota_before_release(tmp_path: Path) -> None:
    row = _row()
    (tmp_path / str(row.id)).symlink_to(tmp_path / "outside")
    owner, global_row = _quotas()
    with pytest.raises(ImageStorageUnavailable):
        input_cleanup.purge_failed_input_bytes(tmp_path, row, owner, global_row)
    assert row.purged_at is None and owner.held_bytes == 7
    (tmp_path / str(row.id)).unlink()
    owner.held_bytes = 0
    with pytest.raises(ImageStorageUnavailable):
        input_cleanup.purge_failed_input_bytes(tmp_path, row, owner, global_row)
    assert row.purged_at is None


def test_normalized_input_deletion_releases_both_private_files_and_full_quota(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    derived = tmp_path / "derived"
    original.mkdir()
    derived.mkdir()
    row = _row()
    row.purpose = "mask"
    row.state = "deleting"
    row.deleted_at = datetime.now(UTC)
    row.held_bytes = 0
    row.normalized_key = str(row.id)
    row.normalized_bytes = 5
    (original / str(row.id)).write_bytes(b"pngdata")
    (derived / str(row.id)).write_bytes(b"mask!")
    owner, global_row = _quotas()
    owner.held_bytes = global_row.held_bytes = 0
    owner.stored_bytes = global_row.stored_bytes = 12
    input_cleanup.purge_deleted_input_bytes(original, row, owner, global_row, derived)
    assert row.state == "purged" and row.purged_at is not None
    assert list(original.iterdir()) == list(derived.iterdir()) == []
    assert owner.stored_bytes == global_row.stored_bytes == 0


def test_failed_normalization_releases_hold_only_after_both_files_are_absent(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    derived = tmp_path / "derived"
    original.mkdir()
    derived.mkdir()
    row = _row()
    row.purpose = "init"
    row.held_bytes = 7 + 10 * 1024 * 1024
    (original / str(row.id)).write_bytes(b"pngdata")
    target = derived / str(row.id)
    target.symlink_to(tmp_path / "outside")
    owner, global_row = _quotas()
    owner.held_bytes = global_row.held_bytes = row.held_bytes
    with pytest.raises(ImageStorageUnavailable):
        input_cleanup.purge_failed_input_bytes(original, row, owner, global_row, derived)
    assert owner.held_bytes == row.held_bytes
    target.unlink()
    input_cleanup.purge_failed_input_bytes(original, row, owner, global_row, derived)
    assert owner.held_bytes == global_row.held_bytes == row.held_bytes == 0


def test_failed_input_maintenance_locks_quota_before_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _row()
    (tmp_path / str(row.id)).write_bytes(b"pngdata")
    owner, global_row = _quotas()
    calls: list[str] = []

    class FakeSession:
        async def get(self, model: type[object], identity: object, **kwargs: object) -> object:
            assert model is ImageInputRow and identity == row.id
            calls.append("row_lock" if kwargs.get("with_for_update") else "snapshot")
            return row

    @asynccontextmanager
    async def scope() -> AsyncIterator[FakeSession]:
        yield FakeSession()

    async def locked(session: object, owner_id: uuid.UUID) -> tuple[ImageQuotaRow, ImageQuotaRow]:
        assert owner_id == OWNER
        calls.append("quotas")
        return global_row, owner

    monkeypatch.setattr(input_cleanup, "session_scope", scope)
    monkeypatch.setattr(input_cleanup, "_locked_rows", locked)
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent", image_input_original_root=str(tmp_path)
    )
    assert asyncio.run(input_cleanup.purge_failed_input(settings, row.id))
    assert calls == ["snapshot", "quotas", "row_lock"]
    assert row.purged_at is not None and owner.held_bytes == global_row.held_bytes == 0


def test_orphan_sweep_preserves_committed_row_and_new_or_symlink_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orphan = uuid.uuid4()
    committed = uuid.uuid4()
    recent = uuid.uuid4()
    for input_id in (orphan, committed, recent):
        (tmp_path / str(input_id)).write_bytes(b"pngdata")
    old = datetime.now(UTC).timestamp() - 7200
    os.utime(tmp_path / str(orphan), (old, old))
    os.utime(tmp_path / str(committed), (old, old))
    (tmp_path / "not-an-id").write_bytes(b"keep")
    symlink = tmp_path / str(uuid.uuid4())
    symlink.symlink_to(tmp_path / "outside")
    os.utime(symlink, (old, old), follow_symlinks=False)

    class FakeSession:
        async def execute(self, statement: object) -> None:
            pass

        async def get(self, model: type[object], identity: object) -> object | None:
            assert model is ImageInputRow
            return object() if identity == committed else None

    @asynccontextmanager
    async def scope() -> AsyncIterator[FakeSession]:
        yield FakeSession()

    monkeypatch.setattr(input_cleanup, "session_scope", scope)
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent", image_input_original_root=str(tmp_path)
    )
    assert asyncio.run(input_cleanup.sweep_orphan_inputs(settings)) == 1
    assert not (tmp_path / str(orphan)).exists()
    assert (tmp_path / str(committed)).exists()
    assert (tmp_path / str(recent)).exists()
    assert symlink.is_symlink()
    assert (tmp_path / "not-an-id").read_bytes() == b"keep"


def test_orphan_input_inventory_refuses_unbounded_unrecognized_entries(tmp_path: Path) -> None:
    for index in range(input_cleanup._MAX_DIRECTORY_ENTRIES + 1):
        (tmp_path / f"unknown-{index}").touch()
    with pytest.raises(ImageStorageUnavailable):
        input_cleanup._old_generated_names(tmp_path)


@pytest.mark.parametrize("kind", ["deleted", "failed"])
async def test_input_purge_sweeps_advance_past_failed_first_batch(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    timestamp = datetime.now(UTC) - timedelta(hours=1)
    rows = [(uuid.UUID(int=index), timestamp) for index in range(1, 27)]
    statements: list[str] = []
    attempted: list[uuid.UUID] = []

    class Session:
        async def execute(self, statement: object) -> SimpleNamespace:
            statements.append(str(statement))
            page = (
                rows[:25]
                if len(statements) in {1, 4}
                else rows[25:]
                if len(statements) == 2
                else []
            )
            return SimpleNamespace(all=lambda: page)

        async def scalar(self, statement: object) -> datetime:
            return timestamp

    @asynccontextmanager
    async def scope() -> AsyncIterator[Session]:
        yield Session()

    async def purge(settings: Settings, input_id: uuid.UUID) -> bool:
        attempted.append(input_id)
        if input_id != rows[-1][0]:
            raise ImageStorageUnavailable()
        return True

    monkeypatch.setattr(input_cleanup, "session_scope", scope)
    monkeypatch.setattr(input_cleanup, f"purge_{kind}_input", purge)
    monkeypatch.setattr(input_cleanup, f"_{kind}_after", None)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    sweep = getattr(input_cleanup, f"sweep_{kind}_inputs")
    assert await sweep(settings) == 0
    assert attempted == [row[0] for row in rows[:25]]
    assert await sweep(settings) == 1
    assert attempted[-1] == rows[-1][0]
    column = "deleted_at" if kind == "deleted" else "created_at"
    assert f"image_inputs.{column}, image_inputs.id" in statements[1]
    assert await sweep(settings) == 0
    assert attempted[-25:] == [row[0] for row in rows[:25]]
