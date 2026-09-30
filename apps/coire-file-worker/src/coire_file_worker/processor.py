"""Bounded CPU-only inspection and rasterization of private chat attachments."""

from __future__ import annotations

import hashlib
import io
import math
import os
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from PIL import Image, ImageOps, UnidentifiedImageError

from coire_core.models.files import FileProcessAsset, FileProcessRequest, FileProcessResult

MAX_ORIGINAL = 10 * 1024 * 1024
MAX_TEXT = 1024 * 1024
MAX_DERIVED = 32 * 1024 * 1024
MAX_PAGES = 50
MAX_UPLOAD_PIXELS = 20_000_000
MAX_OUTPUT_PIXELS = 4_000_000
MAX_OUTPUT_SIDE = 2048
Image.MAX_IMAGE_PIXELS = MAX_UPLOAD_PIXELS


class FileProcessingError(Exception):
    """A stable, content-free error suitable for a private job status."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _check_deadline(request: FileProcessRequest) -> None:
    deadline = request.deadline_at
    if deadline.tzinfo is None or datetime.now(UTC) >= deadline:
        raise FileProcessingError("deadline_exceeded")


def _read_original(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode) or not 0 < file_stat.st_size <= MAX_ORIGINAL:
                raise FileProcessingError("invalid_original_size")
            with os.fdopen(fd, "rb", closefd=False) as source:
                data = source.read(MAX_ORIGINAL + 1)
        finally:
            os.close(fd)
    except (OSError, ValueError) as exc:
        raise FileProcessingError("original_unavailable") from exc
    if not 0 < len(data) <= MAX_ORIGINAL:
        raise FileProcessingError("invalid_original_size")
    return data


def _target_size(width: int, height: int) -> tuple[int, int]:
    if width < 1 or height < 1:
        raise FileProcessingError("invalid_image")
    scale = min(
        1.0, MAX_OUTPUT_SIDE / max(width, height), math.sqrt(MAX_OUTPUT_PIXELS / (width * height))
    )
    return max(1, math.floor(width * scale)), max(1, math.floor(height * scale))


def _normalize(image: Image.Image) -> tuple[bytes, int, int]:
    try:
        if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
            raise FileProcessingError("animated_image_unsupported")
        if image.width * image.height > MAX_UPLOAD_PIXELS:
            raise FileProcessingError("image_too_large")
        image.load()
        oriented = ImageOps.exif_transpose(image)
        target = _target_size(*oriented.size)
        if target != oriented.size:
            oriented.thumbnail(target, Image.Resampling.LANCZOS)
        rgb = oriented.convert("RGB")
        output = io.BytesIO()
        rgb.save(output, format="PNG", optimize=False)
        data = output.getvalue()
        if len(data) > MAX_DERIVED:
            raise FileProcessingError("derived_output_too_large")
        return data, rgb.width, rgb.height
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise FileProcessingError("invalid_image") from exc


def _save_asset(
    root: Path,
    request: FileProcessRequest,
    asset_id: uuid.UUID,
    data: bytes,
    width: int,
    height: int,
    page: int | None = None,
) -> FileProcessAsset:
    job_dir = root / request.job_id
    job_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = job_dir / f"{asset_id}.png"
    temporary = job_dir / f".{asset_id}.{uuid.uuid4()}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as destination:
                destination.write(data)
                destination.flush()
                os.fsync(destination.fileno())
        finally:
            os.close(fd)
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError as exc:
        raise FileProcessingError("derived_output_exists") from exc
    except OSError as exc:
        raise FileProcessingError("derived_output_unavailable") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return FileProcessAsset(
        id=asset_id,
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        media_type="image/png",
        width=width,
        height=height,
        page=page,
    )


def _text(data: bytes) -> str:
    if len(data) > MAX_TEXT:
        raise FileProcessingError("text_too_large")
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileProcessingError("invalid_text_encoding") from exc
    if "\x00" in value:
        raise FileProcessingError("unsupported_file_type")
    return value


def _pdf(
    request: FileProcessRequest,
    data: bytes,
    output_root: Path,
) -> FileProcessResult:
    try:
        document = pdfium.PdfDocument(data)
        try:
            count = len(document)
            if not 1 <= count <= MAX_PAGES:
                raise FileProcessingError("pdf_page_limit")
            if any(page > count for page in request.selected_pages):
                raise FileProcessingError("pdf_page_out_of_range")
            if request.operation == "inspect" and request.selected_pages:
                raise FileProcessingError("invalid_selection")
            chunks: list[str] = []
            total = 0
            assets: list[FileProcessAsset] = []
            for page_number in range(1, count + 1):
                _check_deadline(request)
                page = document[page_number - 1]
                try:
                    if request.operation == "inspect":
                        text_page = page.get_textpage()
                        try:
                            # Refuse before materializing an unbounded native text buffer.
                            if text_page.count_chars() > MAX_TEXT - total:
                                raise FileProcessingError("text_too_large")
                            extracted = text_page.get_text_range()
                        finally:
                            text_page.close()
                        chunk = f"[Page {page_number}]\n{extracted}\n"
                        total += len(chunk.encode("utf-8"))
                        if total > MAX_TEXT:
                            raise FileProcessingError("text_too_large")
                        chunks.append(chunk)
                    elif page_number in request.selected_pages:
                        width, height = page.get_size()
                        target = _target_size(math.ceil(width), math.ceil(height))
                        scale = min(target[0] / width, target[1] / height)
                        bitmap = page.render(scale=scale)
                        try:
                            raster = bitmap.to_pil()
                            try:
                                encoded, pixels_x, pixels_y = _normalize(raster)
                            finally:
                                raster.close()
                        finally:
                            bitmap.close()
                        asset_id = request.output_ids[request.selected_pages.index(page_number)]
                        assets.append(
                            _save_asset(
                                output_root,
                                request,
                                asset_id,
                                encoded,
                                pixels_x,
                                pixels_y,
                                page_number,
                            )
                        )
                        if sum(asset.bytes for asset in assets) > MAX_DERIVED:
                            raise FileProcessingError("derived_output_too_large")
                finally:
                    page.close()
            _check_deadline(request)
            return FileProcessResult(
                job_id=request.job_id,
                input_id=request.input_id,
                source_sha256=request.source_sha256,
                detected_type="application/pdf",
                page_count=count,
                extracted_text="".join(chunks) if request.operation == "inspect" else None,
                assets=assets,
            )
        finally:
            document.close()
    except FileProcessingError:
        raise
    except pdfium.PdfiumError as exc:
        if exc.err_code == pdfium_raw.FPDF_ERR_PASSWORD:
            raise FileProcessingError("pdf_password_required") from exc
        raise FileProcessingError("invalid_pdf") from exc
    except (OSError, ValueError) as exc:
        raise FileProcessingError("invalid_pdf") from exc


def process_file(
    request: FileProcessRequest,
    original_root: Path,
    derived_root: Path,
) -> FileProcessResult:
    """Process a generated-key original; the service enforces its hard process deadline."""

    _check_deadline(request)
    data = _read_original(original_root / str(request.input_id))
    if hashlib.sha256(data).hexdigest() != request.source_sha256:
        raise FileProcessingError("original_digest_mismatch")
    if data.startswith(b"%PDF-"):
        return _pdf(request, data, derived_root)
    if request.operation != "inspect" or request.selected_pages:
        raise FileProcessingError("unsupported_operation")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise FileProcessingError("unsupported_file_type")
            if len(request.output_ids) != 1:
                raise FileProcessingError("invalid_selection")
            encoded, width, height = _normalize(image)
            _check_deadline(request)
            asset = _save_asset(
                derived_root, request, request.output_ids[0], encoded, width, height
            )
            return FileProcessResult(
                job_id=request.job_id,
                input_id=request.input_id,
                source_sha256=request.source_sha256,
                detected_type=f"image/{image.format.lower()}",
                assets=[asset],
            )
    except Image.DecompressionBombError as exc:
        raise FileProcessingError("image_too_large") from exc
    except (UnidentifiedImageError, OSError, ValueError):
        pass
    value = _text(data)
    _check_deadline(request)
    return FileProcessResult(
        job_id=request.job_id,
        input_id=request.input_id,
        source_sha256=request.source_sha256,
        detected_type="text/plain",
        extracted_text=value,
        assets=[],
    )
