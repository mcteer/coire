"""Generation inputs are decoded only in the isolated worker under exact ID bindings."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr, ValidationError

from coire_core.models.files import ImageFileProcessRequest
from coire_core.settings import Settings
from coire_file_worker.app import create_app
from coire_file_worker.image_inputs import ImageInputProcessError, process_image_input

JOB = "01J00000000000000000000000"


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    original = tmp_path / "original"
    derived = tmp_path / "derived"
    original.mkdir(mode=0o700)
    derived.mkdir(mode=0o700)
    return original, derived


def _request(
    path: Path, purpose: str, *, output_id: uuid.UUID | None = None
) -> ImageFileProcessRequest:
    return ImageFileProcessRequest.model_validate(
        {
            "job_id": JOB,
            "input_id": uuid.UUID(path.name),
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "purpose": purpose,
            "operation": "normalize_mask" if purpose == "mask" else "normalize_image",
            "byte_count": path.stat().st_size,
            "output_id": output_id or uuid.uuid4(),
            "deadline_at": datetime.now(UTC) + timedelta(seconds=10),
        }
    )


def test_orientation_and_private_normalized_output(tmp_path: Path) -> None:
    original, derived = _roots(tmp_path)
    source = original / str(uuid.uuid4())
    image = Image.new("RGB", (2, 1))
    image.putpixel((0, 0), (255, 0, 0))
    image.putpixel((1, 0), (0, 0, 255))
    exif = Image.Exif()
    exif[274] = 6
    image.save(source, format="JPEG", exif=exif, quality=100, subsampling=0)
    source.chmod(0o600)
    request = _request(source, "init")
    result = process_image_input(request, original, derived)
    target = derived / str(request.output_id)
    assert (result.width, result.height) == (1, 2)
    assert result.normalized_bytes == target.stat().st_size
    assert target.stat().st_mode & 0o077 == 0
    assert result.normalized_sha256 == hashlib.sha256(target.read_bytes()).hexdigest()
    with Image.open(target) as normalized:
        assert normalized.format == "PNG"
        assert normalized.mode == "RGB"
        upper = cast(tuple[int, int, int], normalized.getpixel((0, 0)))
        lower = cast(tuple[int, int, int], normalized.getpixel((0, 1)))
        assert upper[0] > upper[2]
        assert lower[2] > lower[0]


def test_mask_white_edits_black_keeps_without_inversion(tmp_path: Path) -> None:
    original, derived = _roots(tmp_path)
    source = original / str(uuid.uuid4())
    mask = Image.new("L", (2, 1))
    mask.putpixel((0, 0), 0)
    mask.putpixel((1, 0), 255)
    mask.save(source, format="PNG")
    source.chmod(0o600)
    request = _request(source, "mask")
    process_image_input(request, original, derived)
    with Image.open(derived / str(request.output_id)) as normalized:
        assert normalized.mode == "L"
        assert [normalized.getpixel((x, 0)) for x in range(2)] == [0, 255]


def test_normalization_replay_returns_same_bytes_and_refuses_changed_target(tmp_path: Path) -> None:
    original, derived = _roots(tmp_path)
    source = original / str(uuid.uuid4())
    Image.new("RGB", (4, 4), "red").save(source, format="PNG")
    source.chmod(0o600)
    request = _request(source, "init")
    first = process_image_input(request, original, derived)
    assert process_image_input(request, original, derived) == first
    target = derived / str(request.output_id)
    target.write_bytes(b"changed")
    with pytest.raises(ImageInputProcessError, match="image_output_conflict"):
        process_image_input(request, original, derived)


def test_wrong_digest_forged_id_and_symlink_never_create_derived_bytes(tmp_path: Path) -> None:
    original, derived = _roots(tmp_path)
    source = original / str(uuid.uuid4())
    Image.new("RGB", (2, 2)).save(source, format="PNG")
    source.chmod(0o600)
    request = _request(source, "control")
    for changed in (
        request.model_copy(update={"source_sha256": "0" * 64}),
        request.model_copy(update={"input_id": uuid.uuid4()}),
    ):
        with pytest.raises(ImageInputProcessError):
            process_image_input(changed, original, derived)
    linked = original / str(uuid.uuid4())
    linked.symlink_to(source)
    with pytest.raises(ImageInputProcessError):
        process_image_input(
            _request(source, "init").model_copy(update={"input_id": uuid.UUID(linked.name)}),
            original,
            derived,
        )
    assert list(derived.iterdir()) == []


def test_generation_request_rejects_over_ten_mib_even_when_recipe_cap_is_larger() -> None:
    with pytest.raises(ValidationError):
        ImageFileProcessRequest.model_validate(
            {
                "job_id": JOB,
                "input_id": uuid.uuid4(),
                "source_sha256": "a" * 64,
                "purpose": "init",
                "operation": "normalize_image",
                "byte_count": 10 * 1024 * 1024 + 1,
                "output_id": uuid.uuid4(),
                "deadline_at": datetime.now(UTC) + timedelta(seconds=10),
            }
        )


@pytest.mark.asyncio
async def test_private_normalization_route_requires_service_token(tmp_path: Path) -> None:
    original, derived = _roots(tmp_path)
    source = original / str(uuid.uuid4())
    Image.new("RGB", (4, 4), "blue").save(source, format="PNG")
    source.chmod(0o600)
    request = _request(source, "init")
    settings = Settings(
        file_worker_service_token=SecretStr("private-test-token"),
        file_worker_image_input_root=str(original),
        file_worker_image_output_root=str(derived),
    )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        denied = await client.post("/v1/image-inputs/process", json=request.model_dump(mode="json"))
        assert denied.status_code == 401
        client.headers["Authorization"] = "Bearer private-test-token"
        accepted = await client.post(
            "/v1/image-inputs/process", json=request.model_dump(mode="json")
        )
        assert accepted.status_code == 200
        assert accepted.json()["output_id"] == str(request.output_id)
        changed = await client.post(
            "/v1/image-inputs/process",
            json={**request.model_dump(mode="json"), "source_sha256": "0" * 64},
        )
        assert changed.status_code == 422
        assert changed.json()["detail"] == "image_input_mismatch"
