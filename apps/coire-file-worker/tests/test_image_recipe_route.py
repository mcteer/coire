"""Private recipe parsing uses a generated ID, one descriptor and a service token."""

from __future__ import annotations

import hashlib
import struct
import uuid
import zlib
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from coire_core.models.image_worker import ImageRecipeParseRequest, ImageRecipeParseResult
from coire_core.models.images import (
    ImageRecipe,
    ImageSpec,
    ResolvedImageSpec,
    canonical_recipe_bytes,
    canonical_spec_hash,
)
from coire_core.settings import Settings
from coire_file_worker.app import create_app
from coire_file_worker.image_inputs import ImageRecipeParseError, parse_recipe_png

TOKEN = "worker-test-token"


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _recipe() -> ImageRecipe:
    spec = ImageSpec(
        model_id=uuid.uuid4(),
        prompt="private",
        seed=7,
        width=512,
        height=512,
        steps=20,
        guidance=Decimal("3"),
    )
    return ImageRecipe(
        resolved=ResolvedImageSpec(
            spec=spec,
            seeds=(7,),
            pipeline_version="mflux-0.20.0",
            environment_fingerprint="a" * 64,
            model_sha256="b" * 64,
            spec_hash=canonical_spec_hash(spec),
        ),
        output_index=0,
        seed=7,
        pixel_sha256="c" * 64,
        width=512,
        height=512,
    )


def _png(recipe: ImageRecipe) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + _chunk(b"iTXt", b"coire.image\x00\x00\x00\x00\x00" + canonical_recipe_bytes(recipe))
        + _chunk(b"IDAT", b"x")
        + _chunk(b"IEND", b"")
    )


def test_recipe_parse_contract_rejects_paths_and_oversize() -> None:
    payload = {"input_id": str(uuid.uuid4()), "source_sha256": "a" * 64, "byte_count": 1}
    assert ImageRecipeParseRequest.model_validate(payload).byte_count == 1
    with pytest.raises(ValidationError):
        ImageRecipeParseRequest.model_validate({**payload, "path": "/etc/passwd"})
    with pytest.raises(ValidationError):
        ImageRecipeParseRequest.model_validate({**payload, "byte_count": 64 * 1024 * 1024 + 1})


def test_parser_checks_size_and_digest_on_open_descriptor(tmp_path: Path) -> None:
    recipe = _recipe()
    data = _png(recipe)
    path = tmp_path / "image.png"
    path.write_bytes(data)
    assert (
        parse_recipe_png(
            path, expected_size=len(data), expected_sha256=hashlib.sha256(data).hexdigest()
        )
        == recipe
    )
    with pytest.raises(ImageRecipeParseError, match="recipe_input_mismatch"):
        parse_recipe_png(path, expected_size=len(data) + 1)
    with pytest.raises(ImageRecipeParseError, match="recipe_input_mismatch"):
        parse_recipe_png(path, expected_sha256="0" * 64)


@pytest.mark.asyncio
async def test_private_recipe_route_auth_busy_and_result(tmp_path: Path) -> None:
    originals = tmp_path / "originals"
    images = originals / "images"
    images.mkdir(parents=True)
    input_id = uuid.uuid4()
    recipe = _recipe()
    data = _png(recipe)
    (images / str(input_id)).write_bytes(data)
    settings = Settings(
        file_worker_service_token=SecretStr(TOKEN),
        file_worker_input_root=str(originals),
        file_worker_image_input_root=str(images),
        file_worker_output_root=str(tmp_path / "derived"),
    )
    app = create_app(settings)
    request = ImageRecipeParseRequest(
        input_id=input_id, source_sha256=hashlib.sha256(data).hexdigest(), byte_count=len(data)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        denied = await client.post("/v1/image-recipes/parse", json=request.model_dump(mode="json"))
        assert denied.status_code == 401
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        parsed = await client.post("/v1/image-recipes/parse", json=request.model_dump(mode="json"))
        assert parsed.status_code == 200
        assert ImageRecipeParseResult.model_validate(parsed.json()).recipe == recipe
        app.state.worker.active_job = "01K00000000000000000000000"
        busy = await client.post("/v1/image-recipes/parse", json=request.model_dump(mode="json"))
        assert busy.status_code == 429
        app.state.worker.active_job = None
        bad = await client.post(
            "/v1/image-recipes/parse",
            json={**request.model_dump(mode="json"), "source_sha256": "0" * 64},
        )
        assert bad.status_code == 422
        assert bad.json()["detail"] == "recipe_input_mismatch"
