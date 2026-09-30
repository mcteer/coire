"""Cross-service checks for immutable private file-worker manifests."""

from __future__ import annotations

from coire_core.models.files import FileProcessRequest, FileProcessResult


def validate_result(request: FileProcessRequest, result: FileProcessResult) -> None:
    """Validate worker manifest against the persisted immutable request."""

    if (
        result.job_id != request.job_id
        or result.input_id != request.input_id
        or result.source_sha256 != request.source_sha256
    ):
        raise ValueError("worker source identity mismatch")
    if request.operation == "render":
        if (
            result.detected_type != "application/pdf"
            or result.extracted_text is not None
            or result.page_count is None
            or max(request.selected_pages) > result.page_count
            or len(result.assets) != len(request.selected_pages)
            or any(asset.media_type != "image/png" for asset in result.assets)
            or {asset.id: asset.page for asset in result.assets}
            != dict(zip(request.output_ids, request.selected_pages, strict=True))
        ):
            raise ValueError("worker render manifest mismatch")
        return
    if result.detected_type == "text/plain":
        valid = (
            result.extracted_text is not None and not result.assets and result.page_count is None
        )
    elif result.detected_type == "application/pdf":
        valid = (
            result.extracted_text is not None
            and not result.assets
            and result.page_count is not None
        )
    elif result.detected_type in {"image/png", "image/jpeg", "image/webp"}:
        valid = (
            result.extracted_text is None
            and result.page_count is None
            and len(result.assets) == 1
            and len(request.output_ids) == 1
            and result.assets[0].id == request.output_ids[0]
            and result.assets[0].media_type == "image/png"
            and result.assets[0].page is None
        )
    else:
        valid = False
    if not valid:
        raise ValueError("worker inspect manifest mismatch")
