"""Recipe-only admission owns its staged bytes and quota transaction."""

from __future__ import annotations

import asyncio
import uuid
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.images import inputs
from coire_core.errors import ImageQuotaExceeded
from coire_core.models.files import is_ulid
from coire_core.models.images import GENERATION_INPUT_MAX_BYTES, ImageInputUpload
from coire_core.settings import Settings

OWNER = uuid.uuid4()


class FakeSession:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.rows: list[object] = []
        self.commits = 0
        self.fail_commit = fail_commit

    def add(self, row: object) -> None:
        self.rows.append(row)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError("database unavailable")


def _settings(root: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        image_enabled=True,
        image_input_original_root=str(root),
        image_blob_root=str(root),
    )


def _upload(data: bytes) -> UploadFile:
    return UploadFile(file=BytesIO(data), filename="ignored.png")


async def test_recipe_upload_commits_processing_row_and_generated_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    async def reserve(
        session: AsyncSession, owner: uuid.UUID, amount: int, settings: Settings
    ) -> None:
        assert owner == OWNER
        calls.append(amount)

    monkeypatch.setattr(inputs, "reserve_storage_hold", reserve)
    session = FakeSession()
    metadata = ImageInputUpload(purpose="recipe", filename="source.png", byte_count=7)
    result = await inputs.admit_recipe_upload(
        cast(AsyncSession, session), OWNER, metadata, _upload(b"pngdata"), _settings(tmp_path)
    )
    assert result.state == "processing" and result.byte_count == 7
    assert calls == [7] and session.commits == 1
    assert len(session.rows) == 1
    row = session.rows[0]
    assert row.original_key == str(result.id)  # type: ignore[attr-defined]
    assert is_ulid(row.processing_job_id)  # type: ignore[attr-defined]
    assert (tmp_path / str(result.id)).read_bytes() == b"pngdata"


async def test_generation_upload_holds_original_and_normalized_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    async def reserve(
        session: AsyncSession, owner: uuid.UUID, amount: int, settings: Settings
    ) -> None:
        assert owner == OWNER
        calls.append(amount)

    monkeypatch.setattr(inputs, "reserve_storage_hold", reserve)
    session = FakeSession()
    metadata = ImageInputUpload(purpose="mask", filename="mask.png", byte_count=7)
    result = await inputs.admit_image_input_upload(
        cast(AsyncSession, session), OWNER, metadata, _upload(b"pngdata"), _settings(tmp_path)
    )
    assert result.state == "processing" and result.purpose == "mask"
    assert calls == [7 + GENERATION_INPUT_MAX_BYTES]
    row = session.rows[0]
    assert row.held_bytes == calls[0]  # type: ignore[attr-defined]
    assert row.normalized_key is None  # type: ignore[attr-defined]
    assert (tmp_path / str(result.id)).read_bytes() == b"pngdata"


@pytest.mark.parametrize("fail_commit", [False, True])
async def test_recipe_upload_cleanup_preserves_uncertain_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_commit: bool
) -> None:
    async def refuse(
        session: AsyncSession, owner: uuid.UUID, amount: int, settings: Settings
    ) -> None:
        if not fail_commit:
            raise ImageQuotaExceeded()

    monkeypatch.setattr(inputs, "reserve_storage_hold", refuse)
    session = FakeSession(fail_commit=fail_commit)
    metadata = ImageInputUpload(purpose="recipe", filename="source.png", byte_count=7)
    with pytest.raises(RuntimeError if fail_commit else ImageQuotaExceeded):
        await inputs.admit_recipe_upload(
            cast(AsyncSession, session),
            OWNER,
            metadata,
            _upload(b"pngdata"),
            _settings(tmp_path),
        )
    remaining = await asyncio.to_thread(lambda: list(tmp_path.iterdir()))
    assert len(remaining) == (1 if fail_commit else 0)
