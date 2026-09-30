"""Bounded private image input staging; admission and parsing are separate steps."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from coire_core.errors import (
    ImageConflict,
    ImageInputTooLarge,
    ImageStorageUnavailable,
    ImageValidationError,
)
from coire_core.models.images import ImageInputUpload
from coire_core.settings import Settings

_CHUNK = 64 * 1024


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("image input write failed")
        view = view[count:]


def _open_root(root: Path) -> int:
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ImageStorageUnavailable() from exc


@dataclass(frozen=True, slots=True)
class StagedImageInput:
    id: uuid.UUID
    root: Path
    temporary_key: str
    size: int
    sha256: str

    @property
    def temporary(self) -> Path:
        return self.root / self.temporary_key

    @property
    def target(self) -> Path:
        return self.root / str(self.id)

    def publish(self) -> None:
        """Make bytes durable at a generated key without replacing an existing file."""
        root_fd = _open_root(self.root)
        try:
            os.link(
                self.temporary_key,
                str(self.id),
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
                follow_symlinks=False,
            )
            try:
                os.unlink(self.temporary_key, dir_fd=root_fd)
                os.fsync(root_fd)
            except OSError:
                os.unlink(str(self.id), dir_fd=root_fd)
                raise
        except FileExistsError as exc:
            raise ImageConflict("image input already exists") from exc
        except FileNotFoundError as exc:
            raise ImageConflict("image input staging expired") from exc
        except OSError as exc:
            raise ImageStorageUnavailable() from exc
        finally:
            os.close(root_fd)

    def discard(self) -> None:
        root_fd = _open_root(self.root)
        try:
            with suppress(FileNotFoundError):
                os.unlink(self.temporary_key, dir_fd=root_fd)
        except OSError as exc:
            raise ImageStorageUnavailable() from exc
        finally:
            os.close(root_fd)


async def stage_image_input(
    file: UploadFile, root: Path, metadata: ImageInputUpload, settings: Settings
) -> StagedImageInput:
    """Stream actual bytes under the purpose cap; discard partial bytes on any failure."""
    limit = (
        settings.image_recipe_input_max_bytes
        if metadata.purpose == "recipe"
        else settings.image_generation_input_max_bytes
    )
    if metadata.byte_count > limit:
        raise ImageInputTooLarge()
    root_fd = _open_root(root)
    input_id = uuid.uuid4()
    temporary_key = f".{input_id}.{uuid.uuid4()}.uploading"
    try:
        fd = os.open(
            temporary_key,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
    except OSError as exc:
        os.close(root_fd)
        raise ImageStorageUnavailable() from exc

    size = 0
    digest = hashlib.sha256()
    try:
        while chunk := await file.read(_CHUNK):
            if len(chunk) > _CHUNK:
                raise ImageInputTooLarge()
            size += len(chunk)
            if size > limit:
                raise ImageInputTooLarge()
            digest.update(chunk)
            await asyncio.to_thread(_write_all, fd, chunk)
        if size == 0 or size != metadata.byte_count:
            raise ImageValidationError("image input byte count mismatch")
        await asyncio.to_thread(os.fsync, fd)
    except BaseException as exc:
        os.close(fd)
        try:
            os.unlink(temporary_key, dir_fd=root_fd)
        finally:
            os.close(root_fd)
        if isinstance(exc, OSError):
            raise ImageStorageUnavailable() from exc
        raise
    os.close(fd)
    os.close(root_fd)
    return StagedImageInput(input_id, root, temporary_key, size, digest.hexdigest())
