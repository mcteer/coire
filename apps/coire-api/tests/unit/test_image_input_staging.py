"""Image upload staging enforces actual byte limits and private publication."""

from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
from fastapi import UploadFile

from coire_api.images.inputs import stage_image_input
from coire_core.errors import (
    ImageConflict,
    ImageInputTooLarge,
    ImageStorageUnavailable,
    ImageValidationError,
)
from coire_core.models.images import ImageInputUpload
from coire_core.settings import Settings


def _metadata(purpose: str, size: int) -> ImageInputUpload:
    return ImageInputUpload.model_validate(
        {"purpose": purpose, "filename": "source.png", "byte_count": size}
    )


def _settings(**overrides: object) -> Settings:
    return Settings(_secrets_dir="/nonexistent", **overrides)  # type: ignore[arg-type,call-arg]


def _upload(data: bytes) -> UploadFile:
    return UploadFile(file=BytesIO(data), filename="ignored/../../source.png")


async def test_staging_uses_actual_size_hash_and_exclusive_publish(tmp_path: Path) -> None:
    settings = _settings(image_generation_input_max_bytes=8)
    staged = await stage_image_input(
        _upload(b"12345678"), tmp_path / "images", _metadata("init", 8), settings
    )
    assert staged.size == 8
    assert staged.sha256 == hashlib.sha256(b"12345678").hexdigest()
    assert staged.temporary.parent == tmp_path / "images"
    assert staged.temporary.read_bytes() == b"12345678"
    staged.publish()
    assert staged.target.read_bytes() == b"12345678"
    assert not staged.temporary.exists()
    with pytest.raises(ImageConflict):
        staged.publish()


async def test_generation_limit_uses_actual_bytes_and_cleans_up(tmp_path: Path) -> None:
    root = tmp_path / "images"
    settings = _settings(image_generation_input_max_bytes=8)
    with pytest.raises(ImageInputTooLarge):
        await stage_image_input(_upload(b"123456789"), root, _metadata("mask", 8), settings)
    assert list(root.iterdir()) == []


async def test_recipe_has_separate_limit_and_cannot_be_promoted(tmp_path: Path) -> None:
    root = tmp_path / "images"
    settings = _settings(image_generation_input_max_bytes=8, image_recipe_input_max_bytes=12)
    staged = await stage_image_input(_upload(b"x" * 12), root, _metadata("recipe", 12), settings)
    staged.discard()
    with pytest.raises(ImageInputTooLarge):
        await stage_image_input(_upload(b"x" * 13), root, _metadata("recipe", 12), settings)
    with pytest.raises(ImageInputTooLarge):
        await stage_image_input(_upload(b"x" * 12), root, _metadata("init", 8), settings)
    assert list(root.iterdir()) == []


async def test_declared_count_and_empty_upload_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "images"
    settings = _settings()
    with pytest.raises(ImageValidationError):
        await stage_image_input(_upload(b"short"), root, _metadata("init", 6), settings)
    with pytest.raises(ImageValidationError):
        await stage_image_input(_upload(b""), root, _metadata("init", 1), settings)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("unsafe", ["public", "symlink"])
async def test_staging_refuses_untrusted_root_before_reading_upload(
    tmp_path: Path, unsafe: str
) -> None:
    root = tmp_path / "images"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    if unsafe == "public":
        root.mkdir(mode=0o755)
        root.chmod(0o755)
    else:
        root.symlink_to(outside, target_is_directory=True)

    class UnreadUpload:
        async def read(self, _: int) -> bytes:
            pytest.fail("untrusted storage root accepted upload bytes")

    with pytest.raises(ImageStorageUnavailable):
        await stage_image_input(
            cast(UploadFile, UnreadUpload()), root, _metadata("init", 3), _settings()
        )
    assert list(outside.iterdir()) == []
    assert list(root.iterdir()) == []


async def test_cancelled_read_discards_temporary_file(tmp_path: Path) -> None:
    class InterruptedUpload:
        reads = 0

        async def read(self, _: int) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return b"one"
            raise asyncio.CancelledError

    root = tmp_path / "images"
    with pytest.raises(asyncio.CancelledError):
        await stage_image_input(
            cast(UploadFile, InterruptedUpload()), root, _metadata("init", 3), _settings()
        )
    assert list(root.iterdir()) == []
