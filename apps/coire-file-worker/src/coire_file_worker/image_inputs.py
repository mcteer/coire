"""Isolated, bounded image normalization and settings-only PNG recipe extraction."""

from __future__ import annotations

import hashlib
import io
import os
import stat
from collections.abc import Buffer
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError

from coire_core.image_png import ImageRecipeParseError, parse_recipe_png
from coire_core.models.files import ImageFileProcessRequest, ImageFileProcessResult
from coire_core.models.images import (
    GENERATION_INPUT_MAX_BYTES,
    RECIPE_INPUT_MAX_BYTES,
    canonical_recipe_bytes,
)

_MAX_DIMENSION = 4096
_MAX_PIXELS = _MAX_DIMENSION * _MAX_DIMENSION
_READ_BLOCK = 64 * 1024
_ALLOWED_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})


class ImageInputProcessError(RuntimeError):
    """Content-free refusal code for a private image input."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _open_private_root(root: Path) -> int:
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd)
            raise ImageInputProcessError("image_input_unavailable")
        return fd
    except OSError as exc:
        raise ImageInputProcessError("image_input_unavailable") from exc


def _verified_source(root: Path, request: ImageFileProcessRequest) -> int:
    root_fd = _open_private_root(root)
    fd = -1
    try:
        fd = os.open(str(request.input_id), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
        info = os.fstat(fd)
        limit = (
            RECIPE_INPUT_MAX_BYTES
            if request.operation == "extract_recipe"
            else GENERATION_INPUT_MAX_BYTES
        )
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size != request.byte_count
            or not 0 < info.st_size <= limit
        ):
            raise ImageInputProcessError("image_input_mismatch")
        digest = hashlib.sha256()
        while block := os.read(fd, _READ_BLOCK):
            digest.update(block)
        if digest.hexdigest() != request.source_sha256:
            raise ImageInputProcessError("image_input_mismatch")
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except (OSError, ImageInputProcessError) as exc:
        if fd >= 0:
            os.close(fd)
        if isinstance(exc, ImageInputProcessError):
            raise
        raise ImageInputProcessError("image_input_unavailable") from exc
    finally:
        os.close(root_fd)


def _decode(source: BinaryIO, *, mask: bool) -> Image.Image:
    try:
        with Image.open(source) as original:
            if (
                original.format not in _ALLOWED_FORMATS
                or getattr(original, "is_animated", False)
                or getattr(original, "n_frames", 1) != 1
                or not 1 <= original.width <= _MAX_DIMENSION
                or not 1 <= original.height <= _MAX_DIMENSION
                or original.width * original.height > _MAX_PIXELS
            ):
                raise ImageInputProcessError("image_input_unsupported")
            original.load()
            oriented = ImageOps.exif_transpose(original)
            try:
                if mask:
                    # A white mask pixel edits; black keeps. Never invert it.
                    return oriented.convert("L")
                if oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
                    rgba = oriented.convert("RGBA")
                    try:
                        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                        try:
                            background.alpha_composite(rgba)
                            return background.convert("RGB")
                        finally:
                            background.close()
                    finally:
                        rgba.close()
                return oriented.convert("RGB")
            finally:
                if oriented is not original:
                    oriented.close()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ImageInputProcessError("image_input_invalid") from exc


def _write_normalized(root: Path, output_id: str, image: Image.Image) -> tuple[int, str]:
    class BoundedPng(io.BytesIO):
        def write(self, data: Buffer) -> int:
            if self.tell() + memoryview(data).nbytes > GENERATION_INPUT_MAX_BYTES:
                raise ImageInputProcessError("image_input_too_large")
            return super().write(data)

    payload_buffer = BoundedPng()
    try:
        image.save(payload_buffer, format="PNG", optimize=False, compress_level=6)
        payload = payload_buffer.getvalue()
    except ImageInputProcessError:
        raise
    except (OSError, ValueError) as exc:
        raise ImageInputProcessError("image_input_invalid") from exc
    if not payload:
        raise ImageInputProcessError("image_input_invalid")
    digest = hashlib.sha256(payload).hexdigest()
    try:
        root.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise ImageInputProcessError("image_input_unavailable") from exc
    root_fd = _open_private_root(root)
    fd = -1
    try:
        try:
            fd = os.open(
                output_id,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=root_fd,
            )
        except FileExistsError:
            existing_fd = os.open(output_id, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                info = os.fstat(existing_fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_nlink != 1
                    or info.st_size != len(payload)
                ):
                    raise ImageInputProcessError("image_output_conflict")
                with os.fdopen(existing_fd, "rb", closefd=False) as source:
                    if source.read(len(payload) + 1) != payload:
                        raise ImageInputProcessError("image_output_conflict")
            finally:
                os.close(existing_fd)
            return len(payload), digest
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("normalized input write failed")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.fsync(root_fd)
        return len(payload), digest
    except BaseException as exc:
        if fd >= 0:
            os.close(fd)
            with suppress(FileNotFoundError):
                os.unlink(output_id, dir_fd=root_fd)
        if isinstance(exc, ImageInputProcessError):
            raise
        raise ImageInputProcessError("image_input_unavailable") from exc
    finally:
        os.close(root_fd)


def process_image_input(
    request: ImageFileProcessRequest, input_root: Path, output_root: Path
) -> ImageFileProcessResult:
    """Process only an ID-bound private file; recipe mode never decodes pixels."""
    if request.deadline_at.tzinfo is None or request.deadline_at <= datetime.now(UTC):
        raise ImageInputProcessError("image_input_deadline")
    fd = _verified_source(input_root, request)
    try:
        if request.operation == "extract_recipe":
            try:
                recipe = parse_recipe_png(
                    input_root / str(request.input_id),
                    expected_size=request.byte_count,
                    expected_sha256=request.source_sha256,
                )
            except ImageRecipeParseError as exc:
                raise ImageInputProcessError(exc.code) from exc
            return ImageFileProcessResult(
                job_id=request.job_id,
                input_id=request.input_id,
                operation=request.operation,
                source_sha256=request.source_sha256,
                recipe_json=canonical_recipe_bytes(recipe).decode("utf-8"),
            )
        assert request.output_id is not None
        with os.fdopen(fd, "rb", closefd=False) as source:
            image = _decode(source, mask=request.operation == "normalize_mask")
        try:
            byte_count, sha256 = _write_normalized(output_root, str(request.output_id), image)
            return ImageFileProcessResult(
                job_id=request.job_id,
                input_id=request.input_id,
                operation=request.operation,
                source_sha256=request.source_sha256,
                output_id=request.output_id,
                normalized_sha256=sha256,
                normalized_bytes=byte_count,
                width=image.width,
                height=image.height,
            )
        finally:
            image.close()
    finally:
        os.close(fd)


__all__ = [
    "ImageInputProcessError",
    "ImageRecipeParseError",
    "parse_recipe_png",
    "process_image_input",
]
