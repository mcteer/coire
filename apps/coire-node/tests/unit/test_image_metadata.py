"""Generated PNGs carry exact bounded recipes without upstream metadata."""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image

from coire_core.image_png import parse_recipe_png
from coire_core.models.images import (
    ImageSpec,
    ResolvedImageSpec,
    canonical_recipe_bytes,
    canonical_spec_hash,
    pixel_digest,
)
from coire_node.image_runtime import metadata


def _resolved() -> ResolvedImageSpec:
    spec = ImageSpec(
        model_id=uuid.uuid4(),
        prompt="π private exact values",
        width=64,
        height=64,
        steps=4,
        guidance=Decimal("0.0000"),
        seed=7,
    )
    return ResolvedImageSpec(
        spec=spec,
        seeds=(7,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(spec),
    )


def test_private_png_round_trips_exact_recipe_and_pixel_digest(tmp_path: Path) -> None:
    source = Image.new("RGB", (64, 64), (9, 18, 27))
    source.info["upstream-path"] = "/private/model/weights"
    source.info["icc_profile"] = b"private profile"
    destination = tmp_path / "output.png"
    result = metadata.write_image_png(source, _resolved(), 0, destination)
    assert result.byte_count == destination.stat().st_size
    assert result.sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert result.recipe == parse_recipe_png(destination)
    assert result.recipe.pixel_sha256 == pixel_digest(
        source.tobytes(), width=64, height=64, channels=3
    )
    assert result.recipe.resolved.spec.guidance == Decimal("0.0000")
    assert canonical_recipe_bytes(result.recipe) in destination.read_bytes()
    with Image.open(destination) as saved:
        assert saved.info["coire.image"] == canonical_recipe_bytes(result.recipe).decode("utf-8")
        assert "upstream-path" not in saved.info
        assert "icc_profile" not in saved.info
        assert saved.tobytes() == source.tobytes()
    assert destination.stat().st_mode & 0o777 == 0o600


def test_existing_destination_and_wrong_pixels_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "existing.png"
    path.write_bytes(b"keep")
    with pytest.raises(metadata.ImageMetadataUnavailable):
        metadata.write_image_png(Image.new("RGB", (64, 64)), _resolved(), 0, path)
    assert path.read_bytes() == b"keep"
    path.unlink()
    with pytest.raises(metadata.ImageMetadataUnavailable):
        metadata.write_image_png(Image.new("RGBA", (64, 64)), _resolved(), 0, path)
    with pytest.raises(metadata.ImageMetadataUnavailable):
        metadata.write_image_png(Image.new("RGB", (32, 32)), _resolved(), 0, path)
    assert not path.exists()


def test_size_limit_removes_incomplete_png(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata, "MAX_OUTPUT_BYTES", 100)
    path = tmp_path / "too-large.png"
    with pytest.raises(metadata.ImageMetadataUnavailable):
        metadata.write_image_png(Image.new("RGB", (64, 64)), _resolved(), 0, path)
    assert not path.exists()


def test_metadata_limit_removes_incomplete_png(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(metadata, "canonical_recipe_bytes", lambda recipe: b"x" * (64 * 1024 + 1))
    path = tmp_path / "bad-metadata.png"
    with pytest.raises(metadata.ImageMetadataUnavailable):
        metadata.write_image_png(Image.new("RGB", (64, 64)), _resolved(), 0, path)
    assert not path.exists()


def test_recipe_chunk_is_uncompressed_itxt(tmp_path: Path) -> None:
    path = tmp_path / "itxt.png"
    result = metadata.write_image_png(Image.new("RGB", (64, 64)), _resolved(), 0, path)
    png = path.read_bytes()
    assert png.count(b"coire.image\x00") == 1
    assert b"coire.image\x00\x00\x00\x00\x00" + canonical_recipe_bytes(result.recipe) in png
