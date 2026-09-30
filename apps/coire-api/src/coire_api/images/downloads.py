"""Subject-bound image grants and contained, verified API blob reads."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ImageDownloadGrantRow, ImageOutputRow
from coire_api.images.authorization import require_downloadable_image_output
from coire_core.errors import ImageForbidden, ImageNotFound, ImageStorageUnavailable
from coire_core.models.images import RECIPE_INPUT_MAX_BYTES, ImageDownloadGrant

_GRANT_LIFETIME = timedelta(minutes=5)
_POLICY_VERSION = 1
_READ_BLOCK = 256 * 1024


def _subject_binding(principal: Principal) -> bytes:
    owner = principal.user_id
    if owner is None:
        raise ImageForbidden()
    if principal.kind is PrincipalKind.API_KEY:
        if principal.api_key_id is None or principal.credential_version is None:
            raise ImageForbidden()
        return f"key:{owner}:{principal.api_key_id}:{principal.credential_version}".encode()
    if principal.kind in {PrincipalKind.USER, PrincipalKind.ADMIN}:
        return f"user:{owner}".encode()
    raise ImageForbidden()


def _hash_grant(token: str, principal: Principal) -> str:
    return hashlib.sha256(token.encode("ascii") + b"\x00" + _subject_binding(principal)).hexdigest()


async def issue_download_grant(
    session: AsyncSession, principal: Principal, output_id: uuid.UUID
) -> ImageDownloadGrant:
    """Create one short-lived grant after locking output and live explicit authority."""
    row = await require_downloadable_image_output(session, output_id, principal)
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + _GRANT_LIFETIME
    session.add(
        ImageDownloadGrantRow(
            grant_hash=_hash_grant(token, principal),
            output_id=row.id,
            owner_user_id=row.owner_user_id,
            access_policy_version=_POLICY_VERSION,
            expires_at=expires_at,
        )
    )
    return ImageDownloadGrant(
        output_id=row.id,
        url=f"/api/v1/image-outputs/{row.id}/content#grant={token}",
        expires_at=expires_at,
    )


async def redeem_download_grant(
    session: AsyncSession, principal: Principal, output_id: uuid.UUID, token: str
) -> ImageOutputRow:
    """A URL alone is insufficient; exact subject and live output authority are mandatory."""
    row = await require_downloadable_image_output(session, output_id, principal)
    if len(token) > 64 or not token.isascii():
        raise ImageNotFound()
    grant = await session.get(ImageDownloadGrantRow, _hash_grant(token, principal))
    if (
        grant is None
        or grant.output_id != row.id
        or grant.owner_user_id != row.owner_user_id
        or grant.access_policy_version != _POLICY_VERSION
        or grant.expires_at.tzinfo is None
        or grant.expires_at <= datetime.now(UTC)
    ):
        raise ImageNotFound()
    return row


def _safe_blob_parts(key: str) -> list[str]:
    parts = key.split("/")
    if (
        not key
        or key.startswith("/")
        or len(key) > 128
        or "\\" in key
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in key)
        or not key.endswith(".png")
    ):
        raise ImageStorageUnavailable()
    return parts


def open_verified_blob(root: Path, row: ImageOutputRow) -> int:
    """Walk every path component without symlinks; hash bytes before returning the fd."""
    parts = _safe_blob_parts(row.blob_key)
    parent_fd = -1
    file_fd = -1
    try:
        parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        details = os.fstat(file_fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_size != row.size_bytes
            or not 0 < details.st_size <= RECIPE_INPUT_MAX_BYTES
        ):
            raise ImageStorageUnavailable()
        digest = hashlib.sha256()
        while block := os.read(file_fd, _READ_BLOCK):
            digest.update(block)
        if digest.hexdigest() != row.file_sha256:
            raise ImageStorageUnavailable()
        os.lseek(file_fd, 0, os.SEEK_SET)
        returned = file_fd
        file_fd = -1
        return returned
    except (OSError, ImageStorageUnavailable) as exc:
        raise ImageStorageUnavailable() from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
