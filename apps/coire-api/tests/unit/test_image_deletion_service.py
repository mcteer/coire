"""Owner tombstones deny reads before safe physical purge releases quota."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ImageOutputRow, ImageQuotaRow
from coire_api.images import maintenance
from coire_api.images.deletion import purge_output_blob, tombstone_owned_output
from coire_core.errors import ImageNotFound, ImageStorageUnavailable
from coire_core.settings import Settings

OWNER = uuid.uuid4()


def _output(key: str = "outputs/one.png") -> ImageOutputRow:
    return ImageOutputRow(
        id=uuid.uuid4(),
        job_id="01K00000000000000000000000",
        owner_user_id=OWNER,
        output_index=0,
        blob_key=key,
        size_bytes=7,
        file_sha256="a" * 64,
        pixel_sha256="b" * 64,
        recipe={},
        content_tag="normal",
        classifier_provenance={},
        entitlement_snapshot={},
        state="published",
        created_at=datetime.now(UTC),
    )


class FakeSession:
    def __init__(self, row: ImageOutputRow) -> None:
        self.row = row

    async def get(self, model: type[object], identity: object, **_: object) -> object | None:
        assert model is ImageOutputRow
        return self.row if identity == self.row.id else None


async def test_owner_tombstone_is_idempotent_and_other_user_is_hidden() -> None:
    row = _output()
    session = cast(AsyncSession, FakeSession(row))
    owner = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    other = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    with pytest.raises(ImageNotFound):
        await tombstone_owned_output(session, other, row.id)
    assert row.__dict__.get("deleted_at") is None
    first = await tombstone_owned_output(session, owner, row.id)
    assert first.state == "tombstoned"
    assert row.deleted_at is not None
    deleted_at = row.deleted_at
    second = await tombstone_owned_output(session, owner, row.id)
    assert second.state == "tombstoned"
    assert row.deleted_at == deleted_at


def test_purge_requires_tombstone_and_releases_quota_after_unlink(tmp_path: Path) -> None:
    row = _output()
    (tmp_path / "outputs").mkdir()
    target = tmp_path / "outputs" / "one.png"
    target.write_bytes(b"pngdata")
    owner = cast(ImageQuotaRow, SimpleNamespace(scope="owner", owner_user_id=OWNER, stored_bytes=7))
    global_quota = cast(ImageQuotaRow, SimpleNamespace(scope="global", stored_bytes=7))
    with pytest.raises(ImageStorageUnavailable):
        purge_output_blob(tmp_path, row, owner, global_quota)
    assert target.exists() and owner.stored_bytes == 7
    row.deleted_at = datetime.now(UTC)
    purge_output_blob(tmp_path, row, owner, global_quota)
    assert not target.exists()
    assert row.purged_at is not None
    assert owner.stored_bytes == global_quota.stored_bytes == 0
    purge_output_blob(tmp_path, row, owner, global_quota)
    assert owner.stored_bytes == global_quota.stored_bytes == 0


def test_purge_rejects_unsafe_path_and_preserves_quota(tmp_path: Path) -> None:
    row = _output("../secret.png")
    row.deleted_at = datetime.now(UTC)
    owner = cast(ImageQuotaRow, SimpleNamespace(scope="owner", owner_user_id=OWNER, stored_bytes=7))
    global_quota = cast(ImageQuotaRow, SimpleNamespace(scope="global", stored_bytes=7))
    with pytest.raises(ImageStorageUnavailable):
        purge_output_blob(tmp_path, row, owner, global_quota)
    assert row.purged_at is None and owner.stored_bytes == 7
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "one.png").symlink_to(tmp_path / "outside.png")
    row.blob_key = "outputs/one.png"
    with pytest.raises(ImageStorageUnavailable):
        purge_output_blob(tmp_path, row, owner, global_quota)
    assert row.purged_at is None and owner.stored_bytes == 7


def test_missing_blob_is_retry_safe_after_unlink_before_commit(tmp_path: Path) -> None:
    row = _output()
    row.deleted_at = datetime.now(UTC)
    (tmp_path / "outputs").mkdir()
    owner = cast(ImageQuotaRow, SimpleNamespace(scope="owner", owner_user_id=OWNER, stored_bytes=7))
    global_quota = cast(ImageQuotaRow, SimpleNamespace(scope="global", stored_bytes=7))
    purge_output_blob(tmp_path, row, owner, global_quota)
    assert row.purged_at is not None
    assert owner.stored_bytes == global_quota.stored_bytes == 0


def test_purge_keeps_quota_when_blob_has_another_hard_link(tmp_path: Path) -> None:
    row = _output()
    row.deleted_at = datetime.now(UTC)
    (tmp_path / "outputs").mkdir()
    target = tmp_path / "outputs" / "one.png"
    target.write_bytes(b"pngdata")
    duplicate = tmp_path / "retained.png"
    os.link(target, duplicate)
    owner = cast(ImageQuotaRow, SimpleNamespace(scope="owner", owner_user_id=OWNER, stored_bytes=7))
    global_quota = cast(ImageQuotaRow, SimpleNamespace(scope="global", stored_bytes=7))
    with pytest.raises(ImageStorageUnavailable):
        purge_output_blob(tmp_path, row, owner, global_quota)
    assert target.exists() and duplicate.exists()
    assert row.purged_at is None
    assert owner.stored_bytes == global_quota.stored_bytes == 7


async def test_maintenance_continues_after_one_blob_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [uuid.uuid4(), uuid.uuid4()]

    class ScalarRows:
        def all(self) -> list[uuid.UUID]:
            return ids

    class PendingSession:
        async def scalars(self, statement: object) -> ScalarRows:
            return ScalarRows()

        async def scalar(self, statement: object) -> datetime:
            return datetime.now(UTC)

    @asynccontextmanager
    async def fake_scope() -> AsyncIterator[PendingSession]:
        yield PendingSession()

    called: list[uuid.UUID] = []

    async def fake_purge(settings: Settings, output_id: uuid.UUID) -> bool:
        called.append(output_id)
        if output_id == ids[0]:
            raise ImageStorageUnavailable()
        return True

    monkeypatch.setattr(maintenance, "session_scope", fake_scope)
    monkeypatch.setattr(maintenance, "purge_deleted_output", fake_purge)
    assert await maintenance.sweep_deleted_outputs(Settings(_secrets_dir="/nonexistent")) == 1  # type: ignore[call-arg]
    assert called == ids
