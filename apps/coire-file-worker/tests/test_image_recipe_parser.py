"""Settings-only PNG import never decodes or materializes pixels."""

from __future__ import annotations

import struct
import uuid
import zlib
from decimal import Decimal
from pathlib import Path

import pytest

from coire_core.models.images import (
    ImageRecipe,
    ImageSpec,
    ResolvedImageSpec,
    canonical_recipe_bytes,
    canonical_spec_hash,
)
from coire_file_worker.image_inputs import ImageRecipeParseError, parse_recipe_png

PNG = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _recipe() -> ImageRecipe:
    spec = ImageSpec(
        model_id=uuid.uuid4(),
        prompt="private prompt",
        seed=7,
        width=512,
        height=512,
        steps=20,
        guidance=Decimal("3"),
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(7,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(spec),
    )
    return ImageRecipe(
        resolved=resolved, output_index=0, seed=7, pixel_sha256="c" * 64, width=512, height=512
    )


def _itxt(body: bytes, *, compression: int = 0) -> bytes:
    return _chunk(b"iTXt", b"coire.image\x00" + bytes([compression, 0]) + b"\x00\x00" + body)


def _png(recipe_chunk: bytes, *, idat_bytes: int = 1, extra: bytes = b"") -> bytes:
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    return (
        PNG
        + ihdr
        + recipe_chunk
        + extra
        + _chunk(b"IDAT", b"x" * idat_bytes)
        + _chunk(b"IEND", b"")
    )


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_large_recipe_only_png_passes_without_pixel_decode(tmp_path: Path) -> None:
    recipe = _recipe()
    path = _write(
        tmp_path / "large.png",
        _png(_itxt(canonical_recipe_bytes(recipe)), idat_bytes=11 * 1024 * 1024),
    )
    assert path.stat().st_size > 10 * 1024 * 1024
    assert parse_recipe_png(path) == recipe


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"not a png", "invalid_png"),
        (_png(_itxt(canonical_recipe_bytes(_recipe())))[:-2], "invalid_png"),
        (
            _png(_itxt(canonical_recipe_bytes(_recipe()))).replace(
                b"private prompt", b"private pr0mpt"
            ),
            "invalid_png",
        ),
        (
            _png(
                _itxt(canonical_recipe_bytes(_recipe())),
                extra=_itxt(canonical_recipe_bytes(_recipe())),
            ),
            "duplicate_recipe",
        ),
        (
            _png(_itxt(canonical_recipe_bytes(_recipe()), compression=1)),
            "unsupported_recipe_encoding",
        ),
        (_png(_itxt(b"x" * (64 * 1024 + 1))), "recipe_too_large"),
        (_png(_itxt(b"not-json")), "invalid_recipe"),
        (_png(_chunk(b"tEXt", b"unrelated\x00value")), "missing_recipe"),
    ],
)
def test_parser_refuses_invalid_container_or_metadata(
    tmp_path: Path, data: bytes, code: str
) -> None:
    with pytest.raises(ImageRecipeParseError, match=code):
        parse_recipe_png(_write(tmp_path / "bad.png", data))


def test_parser_refuses_oversized_file_and_symlink(tmp_path: Path) -> None:
    large = tmp_path / "too-large.png"
    with large.open("wb") as target:
        target.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(ImageRecipeParseError, match="recipe_input_too_large"):
        parse_recipe_png(large)
    valid = _write(tmp_path / "valid.png", _png(_itxt(canonical_recipe_bytes(_recipe()))))
    linked = tmp_path / "link.png"
    linked.symlink_to(valid)
    with pytest.raises(ImageRecipeParseError, match="recipe_input_unavailable"):
        parse_recipe_png(linked)
