"""Fenced, bounded Studio-to-core PNG transfer and durable receipt issuance."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import stat
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageJobRow, ImageTransferRow, NodeRow
from coire_api.images.inputs import _write_all
from coire_core.errors import ImageConflict, ImageStorageUnavailable, ImageValidationError
from coire_core.image_png import ImageRecipeParseError, parse_recipe_png
from coire_core.models.image_worker import (
    ImageTransferGrant,
    ImageTransferGrantRequest,
    ImageTransferReceipt,
)
from coire_core.models.images import (
    RECIPE_INPUT_MAX_BYTES,
    ImageJobSettingsSnapshot,
    ImageRecipe,
    ResolvedImageSpec,
    canonical_recipe_bytes,
)
from coire_core.settings import Settings

_CHUNK_LIMIT = 256 * 1024
_GRANT_LIFETIME = timedelta(minutes=5)


def _grant_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def require_bound_image_spec(row: ImageJobRow) -> ResolvedImageSpec:
    """A queued effective spec is insufficient to authorize output bytes."""
    try:
        snapshot = ImageJobSettingsSnapshot.model_validate(row.resolved_spec)
    except ValidationError:
        raise ImageConflict("image runtime settings are unavailable") from None
    if snapshot.resolved is None:
        raise ImageConflict("image runtime is not bound")
    return snapshot.resolved


async def mint_transfer_grant(
    session: AsyncSession, request: ImageTransferGrantRequest
) -> ImageTransferGrant:
    """Scheduler-only domain operation; caller commits before sending the token to a node."""
    job = await session.get(ImageJobRow, request.job_id, with_for_update=True)
    if (
        job is None
        or job.state != "transferring"
        or job.cancel_requested_at is not None
        or job.attempt != request.attempt
        or job.fence != request.fence
        or job.selected_node_id is None
    ):
        raise ImageConflict("image attempt is unavailable")
    node = await session.get(NodeRow, job.selected_node_id)
    if node is None or node.name != request.node:
        raise ImageConflict("image node differs from selected node")
    resolved = require_bound_image_spec(job)
    if request.index >= resolved.spec.n:
        raise ImageConflict("image output index is unavailable")
    row = await session.get(
        ImageTransferRow,
        (request.job_id, request.attempt, request.index),
        with_for_update=True,
    )
    if row is not None and (
        row.expected_bytes != request.expected_bytes
        or row.expected_sha256 != request.expected_sha256
    ):
        raise ImageConflict("image output digest changed")
    token = secrets.token_urlsafe(32)
    issued_at = datetime.now(UTC)
    expires_at = issued_at + _GRANT_LIFETIME
    if row is None:
        row = ImageTransferRow(
            job_id=request.job_id,
            attempt=request.attempt,
            output_index=request.index,
            expected_bytes=request.expected_bytes,
            expected_sha256=request.expected_sha256,
            staging_key=f"image-staging/{request.job_id}/{request.attempt}/{request.index}.png",
            state="pending",
            lease_expires_at=expires_at,
            grant_hash=_grant_hash(token),
        )
        session.add(row)
    else:
        row.grant_hash = _grant_hash(token)
        row.lease_expires_at = expires_at
    return ImageTransferGrant(
        **request.model_dump(), token=token, issued_at=issued_at, expires_at=expires_at
    )


def require_node_credential(node: str, token: str, settings: Settings) -> None:
    """The transfer grant and the Keychain-sourced node credential are both required."""
    expected = settings.node_token_map.get(node, "")
    if not expected or not token or not hmac.compare_digest(expected, token):
        raise ImageConflict("image node credential unavailable")


def _stage_directory(root: Path, job_id: str, attempt: int) -> tuple[int, Path]:
    """Open generated private path components without following symlinks."""
    path = root
    parent_fd = -1
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        root_info = os.fstat(parent_fd)
        if root_info.st_uid != os.getuid() or root_info.st_mode & 0o077:
            raise ImageStorageUnavailable()
        for part in ("image-staging", job_id, str(attempt)):
            with suppress(FileExistsError):
                os.mkdir(part, mode=0o700, dir_fd=parent_fd)
            child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            info = os.fstat(child_fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                os.close(child_fd)
                raise ImageStorageUnavailable()
            os.close(parent_fd)
            parent_fd = child_fd
            path /= part
        return parent_fd, path
    except (OSError, ImageStorageUnavailable) as exc:
        if parent_fd >= 0:
            os.close(parent_fd)
        raise ImageStorageUnavailable() from exc


def _existing_matches(directory: int, name: str, size: int, digest: str) -> bool:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ImageStorageUnavailable() from exc
    try:
        details = os.fstat(fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
            or details.st_size != size
        ):
            return False
        with os.fdopen(fd, "rb", closefd=False) as source:
            return hashlib.file_digest(source, "sha256").hexdigest() == digest
    finally:
        os.close(fd)


def _verify_staged(root: Path, row: ImageTransferRow, receipt: ImageTransferReceipt) -> ImageRecipe:
    directory, path = _stage_directory(root, row.job_id, row.attempt)
    try:
        name = f"{row.output_index}.png"
        if not _existing_matches(directory, name, row.expected_bytes, row.expected_sha256):
            raise ImageStorageUnavailable()
        try:
            recipe = parse_recipe_png(
                path / name,
                expected_size=row.expected_bytes,
                expected_sha256=row.expected_sha256,
            )
        except ImageRecipeParseError:
            raise ImageStorageUnavailable() from None
        if hashlib.sha256(canonical_recipe_bytes(recipe)).hexdigest() != receipt.recipe_sha256:
            raise ImageStorageUnavailable()
        return recipe
    finally:
        os.close(directory)


async def _receive(
    root: Path,
    row: ImageTransferRow,
    body: AsyncIterator[bytes],
    resolved: ResolvedImageSpec,
) -> tuple[str, str]:
    directory, path = await asyncio.to_thread(_stage_directory, root, row.job_id, row.attempt)
    temporary = f".{row.output_index}.{uuid.uuid4().hex}.uploading"
    target = f"{row.output_index}.png"
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            size = 0
            digest = hashlib.sha256()
            async for chunk in body:
                if len(chunk) > _CHUNK_LIMIT:
                    raise ImageValidationError("image output chunk is too large")
                size += len(chunk)
                if size > row.expected_bytes:
                    raise ImageValidationError("image output byte count mismatch")
                digest.update(chunk)
                await asyncio.to_thread(_write_all, fd, chunk)
            if size != row.expected_bytes or digest.hexdigest() != row.expected_sha256:
                raise ImageValidationError("image output digest mismatch")
            await asyncio.to_thread(os.fsync, fd)
        finally:
            os.close(fd)
        try:
            recipe = await asyncio.to_thread(
                parse_recipe_png,
                path / temporary,
                expected_size=row.expected_bytes,
                expected_sha256=row.expected_sha256,
            )
        except ImageRecipeParseError as exc:
            raise ImageValidationError("image output PNG or recipe is invalid") from exc
        if recipe.resolved != resolved or recipe.output_index != row.output_index:
            raise ImageValidationError("image output recipe differs from job")
        recipe_sha256 = hashlib.sha256(canonical_recipe_bytes(recipe)).hexdigest()
        try:
            os.link(
                temporary,
                target,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
        except FileExistsError:
            if not await asyncio.to_thread(
                _existing_matches,
                directory,
                target,
                row.expected_bytes,
                row.expected_sha256,
            ):
                raise ImageConflict("image output already differs from attempt") from None
        await asyncio.to_thread(os.fsync, directory)
        return recipe_sha256, recipe.pixel_sha256
    except OSError as exc:
        raise ImageStorageUnavailable() from exc
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)


async def receive_transfer(
    session: AsyncSession,
    settings: Settings,
    *,
    job_id: str,
    index: int,
    attempt: int,
    fence: int,
    node: str,
    grant_token: str,
    body: AsyncIterator[bytes],
) -> tuple[ImageTransferReceipt, bool]:
    """Validate grant, attempt and bytes; return receipt only after a durable private file."""
    job = await session.get(ImageJobRow, job_id, with_for_update=True)
    row = await session.get(ImageTransferRow, (job_id, attempt, index), with_for_update=True)
    if job is None or row is None or job.selected_node_id is None:
        raise ImageConflict("image transfer unavailable")
    if (
        row.expected_bytes < 1
        or row.expected_bytes > RECIPE_INPUT_MAX_BYTES
        or row.staging_key != f"image-staging/{job_id}/{attempt}/{index}.png"
    ):
        raise ImageStorageUnavailable()
    selected = await session.get(NodeRow, job.selected_node_id)
    if (
        selected is None
        or selected.name != node
        or job.state != "transferring"
        or job.cancel_requested_at is not None
        or job.attempt != attempt
        or job.fence != fence
        or row.lease_expires_at.tzinfo is None
        or row.lease_expires_at <= datetime.now(UTC)
        or not grant_token.isascii()
        or len(grant_token) > 512
        or not hmac.compare_digest(row.grant_hash, _grant_hash(grant_token))
    ):
        raise ImageConflict("image transfer grant unavailable")
    if row.receipt is not None:
        receipt = ImageTransferReceipt.model_validate(row.receipt)
        if (
            receipt.job_id != job_id
            or receipt.attempt != attempt
            or receipt.fence != fence
            or receipt.node != node
            or receipt.index != index
            or receipt.byte_count != row.expected_bytes
            or receipt.sha256 != row.expected_sha256
        ):
            raise ImageStorageUnavailable()
        await asyncio.to_thread(_verify_staged, Path(settings.image_blob_root), row, receipt)
        return receipt, False
    resolved = require_bound_image_spec(job)
    recipe_sha256, _ = await _receive(Path(settings.image_blob_root), row, body, resolved)
    receipt = ImageTransferReceipt(
        job_id=job_id,
        attempt=attempt,
        fence=fence,
        node=node,
        index=index,
        output_id=uuid.uuid4(),
        byte_count=row.expected_bytes,
        sha256=row.expected_sha256,
        recipe_sha256=recipe_sha256,
        verified_at=datetime.now(UTC),
    )
    row.receipt = receipt.model_dump(mode="json")
    row.state = "received"
    return receipt, True
