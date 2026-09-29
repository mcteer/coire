"""Real PDFium/Pillow fixtures for the private CPU processor."""

from __future__ import annotations

import hashlib
import io
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pypdfium2 as pdfium
import pytest
from PIL import Image, PngImagePlugin

from coire_core.models.files import FileProcessRequest
from coire_file_worker.processor import FileProcessingError, process_file

JOB_ID = "01K00000000000000000000000"


def _request(
    originals: Path,
    data: bytes,
    *,
    operation: Literal["inspect", "render"] = "inspect",
    pages: list[int] | None = None,
    output_ids: list[uuid.UUID] | None = None,
) -> FileProcessRequest:
    original_id = uuid.uuid4()
    originals.mkdir()
    (originals / str(original_id)).write_bytes(data)
    return FileProcessRequest(
        job_id=JOB_ID,
        input_id=original_id,
        source_sha256=hashlib.sha256(data).hexdigest(),
        operation=operation,
        selected_pages=pages or [],
        output_ids=output_ids or [],
        deadline_at=datetime.now(UTC) + timedelta(seconds=30),
    )


def _png() -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", (3200, 1600), "red")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private", "should not survive")
    image.save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


def _pdf() -> bytes:
    document = pdfium.PdfDocument.new()
    document.new_page(612, 792)
    output = io.BytesIO()
    document.save(output)
    document.close()
    return output.getvalue()


def test_text_and_digest(tmp_path: Path) -> None:
    request = _request(tmp_path / "originals", b"hello \xe2\x98\x83", output_ids=[uuid.uuid4()])
    result = process_file(request, tmp_path / "originals", tmp_path / "derived")
    assert result.detected_type == "text/plain"
    assert result.extracted_text == "hello ☃"
    assert result.assets == []
    altered = request.model_copy(update={"source_sha256": "0" * 64})
    with pytest.raises(FileProcessingError, match="original_digest_mismatch"):
        process_file(altered, tmp_path / "originals", tmp_path / "derived")


@pytest.mark.parametrize(
    "content,code", [(b"\xff", "invalid_text_encoding"), (b"a\x00b", "unsupported_file_type")]
)
def test_bad_text(tmp_path: Path, content: bytes, code: str) -> None:
    request = _request(tmp_path / "originals", content)
    with pytest.raises(FileProcessingError, match=code):
        process_file(request, tmp_path / "originals", tmp_path / "derived")


def test_image_is_oriented_bounded_and_stripped(tmp_path: Path) -> None:
    asset_id = uuid.uuid4()
    request = _request(tmp_path / "originals", _png(), output_ids=[asset_id])
    result = process_file(request, tmp_path / "originals", tmp_path / "derived")
    assert result.detected_type == "image/png"
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.id == asset_id
    assert (asset.width, asset.height) == (2048, 1024)
    output = tmp_path / "derived" / JOB_ID / f"{asset_id}.png"
    assert hashlib.sha256(output.read_bytes()).hexdigest() == asset.sha256
    with Image.open(output) as image:
        assert image.info == {}
        assert image.mode == "RGB"


def test_reject_animated_image(tmp_path: Path) -> None:
    frames = [Image.new("RGB", (8, 8), "red"), Image.new("RGB", (8, 8), "blue")]
    output = io.BytesIO()
    frames[0].save(output, format="WEBP", save_all=True, append_images=frames[1:])
    request = _request(tmp_path / "originals", output.getvalue(), output_ids=[uuid.uuid4()])
    with pytest.raises(FileProcessingError, match="animated_image_unsupported"):
        process_file(request, tmp_path / "originals", tmp_path / "derived")


def test_reject_image_above_pixel_cap(tmp_path: Path) -> None:
    output = io.BytesIO()
    Image.new("RGB", (5000, 4100), "blue").save(output, format="PNG")
    request = _request(tmp_path / "originals", output.getvalue(), output_ids=[uuid.uuid4()])
    with pytest.raises(FileProcessingError, match="image_too_large"):
        process_file(request, tmp_path / "originals", tmp_path / "derived")


def test_scanned_pdf_inspect_and_render(tmp_path: Path) -> None:
    data = _pdf()
    originals = tmp_path / "originals"
    inspect = _request(originals, data, output_ids=[uuid.uuid4()])
    result = process_file(inspect, originals, tmp_path / "derived")
    assert result.detected_type == "application/pdf"
    assert result.page_count == 1
    assert result.extracted_text == "[Page 1]\n\n"
    asset_id = uuid.uuid4()
    render = inspect.model_copy(
        update={"operation": "render", "selected_pages": [1], "output_ids": [asset_id]}
    )
    rendered = process_file(render, originals, tmp_path / "derived")
    assert rendered.assets[0].page == 1
    assert rendered.assets[0].width <= 2048
    assert (tmp_path / "derived" / JOB_ID / f"{asset_id}.png").is_file()


def test_pdf_unicode_extraction_has_page_attribution(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "hello_world.pdf"
    request = _request(tmp_path / "originals", fixture.read_bytes())
    result = process_file(request, tmp_path / "originals", tmp_path / "derived")
    assert result.detected_type == "application/pdf"
    assert result.extracted_text is not None
    assert result.extracted_text.startswith("[Page 1]\n")
    assert "Hello, world!" in result.extracted_text


def test_bad_pdf_and_missing_page(tmp_path: Path) -> None:
    originals = tmp_path / "originals"
    request = _request(originals, b"%PDF-this is corrupt")
    with pytest.raises(FileProcessingError, match="invalid_pdf"):
        process_file(request, originals, tmp_path / "derived")
    original = _request(tmp_path / "other_originals", _pdf())
    render = original.model_copy(
        update={"operation": "render", "selected_pages": [2], "output_ids": [uuid.uuid4()]}
    )
    with pytest.raises(FileProcessingError, match="pdf_page_out_of_range"):
        process_file(render, tmp_path / "other_originals", tmp_path / "derived")


def test_password_protected_pdf_has_a_specific_safe_failure(tmp_path: Path) -> None:
    encrypted = Path(__file__).parent / "fixtures" / "encrypted_hello_world_r3.pdf"
    request = _request(tmp_path / "originals", encrypted.read_bytes())
    with pytest.raises(FileProcessingError, match="pdf_password_required"):
        process_file(request, tmp_path / "originals", tmp_path / "derived")


def test_native_watchdog_exits_a_stuck_worker_process() -> None:
    script = """
import time
import uuid
from datetime import UTC, datetime, timedelta
from coire_core.models.files import FileProcessRequest
from coire_core.settings import Settings
import coire_file_worker.app as worker_module

worker_module.process_file = lambda *_args: time.sleep(5)
request = FileProcessRequest(
    job_id="01K00000000000000000000000",
    input_id=uuid.uuid4(),
    source_sha256="a" * 64,
    operation="inspect",
    deadline_at=datetime.now(UTC) + timedelta(seconds=0.3),
)
worker_module.Worker(Settings(_secrets_dir="/nonexistent"))._process_with_watchdog(request)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 124


def test_original_symlink_refused(tmp_path: Path) -> None:
    originals = tmp_path / "originals"
    request = _request(originals, b"safe")
    original = originals / str(request.input_id)
    target = tmp_path / "target"
    original.rename(target)
    original.symlink_to(target)
    with pytest.raises(FileProcessingError, match="original_unavailable"):
        process_file(request, originals, tmp_path / "derived")


def test_expired_job_refused_before_file_read(tmp_path: Path) -> None:
    request = _request(tmp_path / "originals", b"content")
    expired = request.model_copy(update={"deadline_at": datetime.now(UTC) - timedelta(seconds=1)})
    with pytest.raises(FileProcessingError, match="deadline_exceeded"):
        process_file(expired, tmp_path / "originals", tmp_path / "derived")


def test_derived_asset_never_overwritten(tmp_path: Path) -> None:
    asset_id = uuid.uuid4()
    request = _request(tmp_path / "originals", _png(), output_ids=[asset_id])
    target = tmp_path / "derived" / JOB_ID / f"{asset_id}.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous")
    with pytest.raises(FileProcessingError, match="derived_output_exists"):
        process_file(request, tmp_path / "originals", tmp_path / "derived")
    assert target.read_bytes() == b"previous"
