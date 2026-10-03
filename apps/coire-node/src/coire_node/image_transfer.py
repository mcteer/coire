"""Exact-fence Studio output push to the configured core API."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx

from coire_core.image_png import ImageRecipeParseError, parse_recipe_png
from coire_core.models.image_worker import (
    ImageTransferGrant,
    ImageTransferReceipt,
    NodeImageJob,
    NodeImageStartRequest,
    NodeImageTransferRequest,
)
from coire_core.models.images import canonical_recipe_bytes
from coire_core.settings import Settings
from coire_node.image_cleanup import ImageCleanupUnavailable, _private_dir
from coire_node.metrics import ImageNodeOutcome, ImageNodeStage, image_node_span, record_image_stage

_BLOCK = 64 * 1024


class ImageTransferUnavailable(RuntimeError):
    """Keep scratch and reservation while a transfer or receipt is uncertain."""


def _verify_grant_file(
    scratch: Path, grant: ImageTransferGrant, original: NodeImageStartRequest, recipe_digest: str
) -> Path:
    path = scratch / f"{grant.index}.png"
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ImageTransferUnavailable()
        recipe = parse_recipe_png(
            path, expected_size=grant.expected_bytes, expected_sha256=grant.expected_sha256
        )
    except (OSError, ImageRecipeParseError):
        raise ImageTransferUnavailable() from None
    if (
        recipe.resolved != original.resolved
        or recipe.output_index != grant.index
        or hashlib.sha256(canonical_recipe_bytes(recipe)).hexdigest() != recipe_digest
    ):
        raise ImageTransferUnavailable()
    return path


async def _file_body(path: Path, size: int) -> AsyncIterator[bytes]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ImageTransferUnavailable() from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size != size
        ):
            raise ImageTransferUnavailable()
        while block := await asyncio.to_thread(os.read, fd, _BLOCK):
            yield block
    finally:
        os.close(fd)


async def push_image_outputs(
    state_dir: Path,
    current: NodeImageJob,
    original: NodeImageStartRequest,
    request: NodeImageTransferRequest,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[ImageTransferReceipt, ...]:
    """Return a complete receipt set; no scratch deletion happens here."""
    if (
        current.state != "transferring"
        or (current.job_id, current.attempt, current.fence, current.node)
        != (request.job_id, request.attempt, request.fence, request.node)
        or (original.job_id, original.attempt, original.fence, original.node)
        != (request.job_id, request.attempt, request.fence, request.node)
        or {grant.index for grant in request.grants} != set(range(original.resolved.spec.n))
        or {output.index for output in current.outputs} != set(range(original.resolved.spec.n))
        or not settings.node_token.get_secret_value()
    ):
        raise ImageTransferUnavailable()
    now = datetime.now(UTC)
    outputs = {output.index: output for output in current.outputs}
    scratch_root = state_dir / "image-scratch"
    scratch = scratch_root / f"{request.job_id}-{request.attempt}-{request.fence}"
    try:
        _private_dir(scratch_root)
        _private_dir(scratch)
    except ImageCleanupUnavailable:
        raise ImageTransferUnavailable() from None
    paths: dict[int, Path] = {}
    for grant in request.grants:
        manifest = outputs[grant.index]
        if (
            grant.expires_at <= now
            or grant.expected_bytes != manifest.byte_count
            or grant.expected_sha256 != manifest.sha256
        ):
            raise ImageTransferUnavailable()
        paths[grant.index] = _verify_grant_file(scratch, grant, original, manifest.recipe_sha256)
    receipts: list[ImageTransferReceipt] = []
    with image_node_span(ImageNodeStage.TRANSFER, job_id=request.job_id):
        try:
            async with httpx.AsyncClient(
                transport=transport, trust_env=False, timeout=60.0
            ) as client:
                for grant in sorted(request.grants, key=lambda item: item.index):
                    response = await client.put(
                        f"{settings.image_transfer_api_url.rstrip('/')}/api/v1/internal/images/"
                        f"{request.job_id}/outputs/{grant.index}",
                        headers={
                            "Authorization": f"Bearer {settings.node_token.get_secret_value()}",
                            "x-coire-node": request.node,
                            "x-coire-attempt": str(request.attempt),
                            "x-coire-fence": str(request.fence),
                            "x-coire-transfer-grant": grant.token,
                            "content-type": "image/png",
                        },
                        content=_file_body(paths[grant.index], grant.expected_bytes),
                    )
                    if response.status_code not in {200, 201}:
                        raise ImageTransferUnavailable()
                    receipt = ImageTransferReceipt.model_validate(response.json())
                    if (
                        receipt.job_id != request.job_id
                        or receipt.attempt != request.attempt
                        or receipt.fence != request.fence
                        or receipt.node != request.node
                        or receipt.index != grant.index
                        or receipt.byte_count != grant.expected_bytes
                        or receipt.sha256 != grant.expected_sha256
                        or receipt.recipe_sha256 != outputs[grant.index].recipe_sha256
                    ):
                        raise ImageTransferUnavailable()
                    receipts.append(
                        receipt.model_copy(
                            update={"classification": outputs[grant.index].classification}
                        )
                    )
        except (httpx.HTTPError, ValueError, ImageTransferUnavailable, OSError):
            record_image_stage(
                ImageNodeStage.TRANSFER, ImageNodeOutcome.FAILED, job_id=request.job_id
            )
            raise ImageTransferUnavailable() from None
        record_image_stage(
            ImageNodeStage.TRANSFER, ImageNodeOutcome.SUCCEEDED, job_id=request.job_id
        )
    return tuple(receipts)
