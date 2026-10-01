"""Canonical private PNG output and exact Coire recipe serialization."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

from PIL import Image, PngImagePlugin

from coire_core.models.images import (
    RECIPE_METADATA_MAX_BYTES,
    ImageRecipe,
    ResolvedImageSpec,
    canonical_recipe_bytes,
    pixel_digest,
)

MAX_OUTPUT_BYTES = 64 * 1024 * 1024


class ImageMetadataUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("image output unavailable")


@dataclass(frozen=True)
class EncodedImageOutput:
    recipe: ImageRecipe
    byte_count: int
    sha256: str


class _BoundedHashWriter:
    def __init__(self, target: BinaryIO) -> None:
        self.target = target
        self.byte_count = 0
        self.digest = hashlib.sha256()

    def write(self, payload: bytes) -> int:
        if self.byte_count + len(payload) > MAX_OUTPUT_BYTES:
            raise ImageMetadataUnavailable()
        written = self.target.write(payload)
        if written != len(payload):
            raise ImageMetadataUnavailable()
        self.byte_count += written
        self.digest.update(payload)
        return written

    def flush(self) -> None:
        self.target.flush()

    def tell(self) -> int:
        return self.byte_count


def write_image_png(
    image: Image.Image, resolved: ResolvedImageSpec, output_index: int, path: Path
) -> EncodedImageOutput:
    """Create one 0600 PNG exclusively in node-owned scratch, removing partial output."""
    try:
        if (
            path.suffix != ".png"
            or image.mode != "RGB"
            or image.size != (resolved.spec.width, resolved.spec.height)
            or not 0 <= output_index < len(resolved.seeds)
        ):
            raise ImageMetadataUnavailable()
        pixels = image.tobytes()
        recipe = ImageRecipe(
            resolved=resolved,
            output_index=output_index,
            seed=resolved.seeds[output_index],
            pixel_sha256=pixel_digest(pixels, width=image.width, height=image.height, channels=3),
            width=image.width,
            height=image.height,
        )
        recipe_bytes = canonical_recipe_bytes(recipe)
        if not 0 < len(recipe_bytes) <= RECIPE_METADATA_MAX_BYTES:
            raise ImageMetadataUnavailable()
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_itxt("coire.image", recipe_bytes.decode("utf-8"), zip=False)
        clean_image = Image.frombytes("RGB", image.size, pixels)
    except (ValueError, OSError) as exc:
        raise ImageMetadataUnavailable() from exc

    created = False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        with os.fdopen(fd, "wb") as target:
            writer = _BoundedHashWriter(target)
            clean_image.save(
                cast("BinaryIO", writer),
                format="PNG",
                pnginfo=pnginfo,
                optimize=False,
                compress_level=6,
            )
            writer.flush()
            os.fsync(target.fileno())
            if writer.byte_count == 0:
                raise ImageMetadataUnavailable()
            return EncodedImageOutput(
                recipe=recipe, byte_count=writer.byte_count, sha256=writer.digest.hexdigest()
            )
    except Exception as exc:
        if created:
            path.unlink(missing_ok=True)
        raise ImageMetadataUnavailable() from exc
