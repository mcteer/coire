"""Bounded private image input staging; admission and parsing are separate steps."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.chat.files import new_job_id
from coire_api.db import ImageInputRow
from coire_api.images.quota import reserve_storage_hold
from coire_core.errors import (
    ImageConflict,
    ImageInputTooLarge,
    ImageStorageUnavailable,
    ImageUnsupportedInput,
    ImageValidationError,
)
from coire_core.models.images import GENERATION_INPUT_MAX_BYTES, ImageInput, ImageInputUpload
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


def project_image_input(row: ImageInputRow) -> ImageInput:
    if row.original_sha256 is None:
        raise ImageStorageUnavailable()
    normalized = row.purpose != "recipe" and row.state == "ready"
    if normalized and (
        row.normalized_sha256 is None
        or row.normalized_bytes is None
        or row.normalized_width is None
        or row.normalized_height is None
    ):
        raise ImageStorageUnavailable()
    return ImageInput.model_validate(
        {
            "id": row.id,
            "purpose": row.purpose,
            "state": row.state,
            "byte_count": row.normalized_bytes if normalized else row.original_bytes,
            "sha256": row.normalized_sha256 if normalized else row.original_sha256,
            "width": row.normalized_width if normalized else None,
            "height": row.normalized_height if normalized else None,
            "safe_error": "recipe_invalid" if row.state == "failed" else None,
            "created_at": row.created_at,
        }
    )


async def admit_image_input_upload(
    session: AsyncSession,
    owner_id: uuid.UUID,
    metadata: ImageInputUpload,
    file: UploadFile,
    settings: Settings,
) -> ImageInput:
    """Stage any declared purpose, holding room for normalization before publication."""
    staged = await stage_image_input(
        file, Path(settings.image_input_original_root), metadata, settings
    )
    try:
        held = staged.size + (0 if metadata.purpose == "recipe" else GENERATION_INPUT_MAX_BYTES)
        await reserve_storage_hold(session, owner_id, held, settings)
        now = datetime.now(UTC)
        row = ImageInputRow(
            id=staged.id,
            owner_user_id=owner_id,
            purpose=metadata.purpose,
            original_key=str(staged.id),
            original_bytes=staged.size,
            original_sha256=staged.sha256,
            state="processing",
            recipe=None,
            processing_job_id=new_job_id(),
            held_bytes=held,
            active_references=0,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.flush()
        staged.publish()
        await session.commit()
        return project_image_input(row)
    except BaseException:
        staged.discard()
        raise


async def admit_recipe_upload(
    session: AsyncSession,
    owner_id: uuid.UUID,
    metadata: ImageInputUpload,
    file: UploadFile,
    settings: Settings,
) -> ImageInput:
    """Compatibility entrypoint for existing recipe-only callers."""
    if metadata.purpose != "recipe":
        raise ImageUnsupportedInput()
    return await admit_image_input_upload(session, owner_id, metadata, file, settings)
