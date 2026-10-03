"""Bounded, metadata-only PNG recipe extraction shared by file-worker and Studio node."""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import zlib
from pathlib import Path
from typing import BinaryIO

from coire_core.models.images import (
    RECIPE_INPUT_MAX_BYTES,
    RECIPE_METADATA_MAX_BYTES,
    ImageRecipe,
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_RECIPE_KEY = b"coire.image\x00"
_STREAM_BLOCK = 64 * 1024
_TEXT_PREFIX = 80
_MAX_CHUNKS = 10_000


class ImageRecipeParseError(Exception):
    """Stable, content-free failure code for an untrusted recipe-only PNG."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read_exact(source: BinaryIO, count: int) -> bytes:
    data = source.read(count)
    if len(data) != count:
        raise ImageRecipeParseError("invalid_png")
    return data


def _read_payload(source: BinaryIO, kind: bytes, length: int) -> tuple[bytes | None, int]:
    """Capture only the bounded recipe candidate; stream every other chunk's CRC."""
    crc = zlib.crc32(kind)
    prefix = (
        _read_exact(source, min(length, _TEXT_PREFIX))
        if kind in {b"IHDR", b"iTXt", b"tEXt", b"zTXt"}
        else b""
    )
    crc = zlib.crc32(prefix, crc)
    remaining = length - len(prefix)
    recipe_candidate = prefix.startswith(_RECIPE_KEY)
    if recipe_candidate and kind in {b"tEXt", b"zTXt"}:
        raise ImageRecipeParseError("unsupported_recipe_encoding")
    if recipe_candidate and kind == b"iTXt":
        if length > RECIPE_METADATA_MAX_BYTES + len(_RECIPE_KEY) + 4:
            raise ImageRecipeParseError("recipe_too_large")
        rest = _read_exact(source, remaining)
        crc = zlib.crc32(rest, crc)
        return prefix + rest, crc
    if kind == b"IHDR":
        return prefix, crc
    while remaining:
        block = _read_exact(source, min(remaining, _STREAM_BLOCK))
        crc = zlib.crc32(block, crc)
        remaining -= len(block)
    return None, crc


def _parse_itxt(payload: bytes) -> ImageRecipe:
    if (
        not payload.startswith(_RECIPE_KEY)
        or payload[len(_RECIPE_KEY) : len(_RECIPE_KEY) + 4] != b"\x00\x00\x00\x00"
    ):
        raise ImageRecipeParseError("unsupported_recipe_encoding")
    recipe_bytes = payload[len(_RECIPE_KEY) + 4 :]
    if not 0 < len(recipe_bytes) <= RECIPE_METADATA_MAX_BYTES:
        raise ImageRecipeParseError("recipe_too_large")
    try:
        return ImageRecipe.model_validate_json(recipe_bytes)
    except ValueError as exc:
        raise ImageRecipeParseError("invalid_recipe") from exc


def _parse_stream(source: BinaryIO, file_size: int) -> ImageRecipe:
    if _read_exact(source, len(_PNG_SIGNATURE)) != _PNG_SIGNATURE:
        raise ImageRecipeParseError("invalid_png")
    seen_ihdr = False
    seen_plte = False
    seen_idat = False
    idat_finished = False
    recipe: ImageRecipe | None = None
    chunk_count = 0
    while source.tell() < file_size:
        chunk_count += 1
        if chunk_count > _MAX_CHUNKS:
            raise ImageRecipeParseError("invalid_png")
        if file_size - source.tell() < 12:
            raise ImageRecipeParseError("invalid_png")
        length = struct.unpack(">I", _read_exact(source, 4))[0]
        kind = _read_exact(source, 4)
        if (
            len(kind) != 4
            or not all(65 <= char <= 90 or 97 <= char <= 122 for char in kind)
            or length > file_size - source.tell() - 4
        ):
            raise ImageRecipeParseError("invalid_png")
        if not seen_ihdr and kind != b"IHDR":
            raise ImageRecipeParseError("invalid_png")
        if kind == b"IHDR":
            if seen_ihdr or length != 13:
                raise ImageRecipeParseError("invalid_png")
            seen_ihdr = True
        elif kind == b"PLTE":
            if seen_plte or seen_idat or length == 0 or length > 768 or length % 3:
                raise ImageRecipeParseError("invalid_png")
            seen_plte = True
        elif kind == b"IDAT":
            if idat_finished:
                raise ImageRecipeParseError("invalid_png")
            seen_idat = True
        elif seen_idat:
            idat_finished = True
        payload, actual_crc = _read_payload(source, kind, length)
        if actual_crc != struct.unpack(">I", _read_exact(source, 4))[0]:
            raise ImageRecipeParseError("invalid_png")
        if kind == b"IHDR":
            if payload is None:
                raise ImageRecipeParseError("invalid_png")
            header = payload
            width, height = struct.unpack(">II", header[:8])
            allowed_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                width == 0
                or height == 0
                or header[8] not in allowed_depths.get(header[9], set())
                or header[10] != 0
                or header[11] != 0
                or header[12] not in {0, 1}
            ):
                raise ImageRecipeParseError("invalid_png")
        elif kind == b"iTXt" and payload is not None:
            if recipe is not None:
                raise ImageRecipeParseError("duplicate_recipe")
            recipe = _parse_itxt(payload)
        elif kind == b"IEND":
            if length != 0 or not seen_idat or source.tell() != file_size:
                raise ImageRecipeParseError("invalid_png")
            if recipe is None:
                raise ImageRecipeParseError("missing_recipe")
            return recipe
        elif kind[0] & 0x20 == 0 and kind not in {b"PLTE", b"IDAT"}:
            raise ImageRecipeParseError("invalid_png")
    raise ImageRecipeParseError("invalid_png")


def parse_recipe_png(
    path: Path, *, expected_size: int | None = None, expected_sha256: str | None = None
) -> ImageRecipe:
    """Read a private staged PNG at <=64 MiB without decoding or retaining IDAT bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ImageRecipeParseError("recipe_input_unavailable") from exc
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode):
            raise ImageRecipeParseError("recipe_input_unavailable")
        if not 0 < details.st_size <= RECIPE_INPUT_MAX_BYTES:
            raise ImageRecipeParseError("recipe_input_too_large")
        if expected_size is not None and details.st_size != expected_size:
            raise ImageRecipeParseError("recipe_input_mismatch")
        with os.fdopen(fd, "rb", closefd=False) as source:
            recipe = _parse_stream(source, details.st_size)
            if expected_sha256 is not None:
                source.seek(0)
                if hashlib.file_digest(source, "sha256").hexdigest() != expected_sha256:
                    raise ImageRecipeParseError("recipe_input_mismatch")
            return recipe
    except OSError as exc:
        raise ImageRecipeParseError("recipe_input_unavailable") from exc
    finally:
        os.close(fd)
