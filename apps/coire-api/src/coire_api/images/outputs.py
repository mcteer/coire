"""Owner-scoped published image metadata and stable gallery pagination."""

from __future__ import annotations

import base64
import binascii
import struct
import uuid
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import ImageOutputRow
from coire_api.images.authorization import (
    authorize_live_image_action,
    output_visible_to_owner,
    require_downloadable_image_output,
)
from coire_core.errors import ImageForbidden, ImageNotFound, ImageValidationError
from coire_core.models.images import (
    ImageClassifierDiagnostic,
    ImageContentTag,
    ImageOutput,
    ImageOutputPage,
    ImageRecipe,
)

_CURSOR_VERSION = 1
_CURSOR_SIZE = 41
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _encode_cursor(row: ImageOutputRow) -> str:
    elapsed = row.created_at.astimezone(UTC) - _EPOCH
    micros = elapsed.days * 86_400_000_000 + elapsed.seconds * 1_000_000 + elapsed.microseconds
    payload = struct.pack(">BQ", _CURSOR_VERSION, micros) + row.owner_user_id.bytes + row.id.bytes
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str, owner_id: uuid.UUID) -> tuple[datetime, uuid.UUID]:
    try:
        if len(cursor) > 100:
            raise ValueError("cursor too long")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if len(raw) != _CURSOR_SIZE or raw[0] != _CURSOR_VERSION:
            raise ValueError("invalid cursor version or size")
        _, micros = struct.unpack(">BQ", raw[:9])
        encoded_owner = uuid.UUID(bytes=raw[9:25])
        output_id = uuid.UUID(bytes=raw[25:41])
        if encoded_owner != owner_id:
            raise ValueError("cursor owner differs")
        return _EPOCH + timedelta(microseconds=micros), output_id
    except (ValueError, OverflowError, binascii.Error) as exc:
        raise ImageNotFound() from exc


def _classifier_diagnostic(row: ImageOutputRow) -> ImageClassifierDiagnostic | None:
    provenance = row.classifier_provenance or {}
    if provenance.get("status") == "unavailable":
        return "classifier_unavailable"
    safe_error = provenance.get("safe_error")
    for code in (
        "classifier_failed",
        "classifier_timeout",
        "classifier_memory",
        "classifier_invalid_result",
    ):
        if safe_error == code:
            return code
    return "classifier_failed" if row.content_tag == ImageContentTag.UNKNOWN else None


def output_projection(row: ImageOutputRow) -> ImageOutput:
    """Expose the exact recipe and digest, never an internal blob path or grant."""
    try:
        return ImageOutput(
            id=row.id,
            job_id=row.job_id,
            index=row.output_index,
            recipe=ImageRecipe.model_validate(row.recipe),
            tag=ImageContentTag(row.content_tag),
            classifier_diagnostic=_classifier_diagnostic(row),
            byte_count=row.size_bytes,
            file_sha256=row.file_sha256,
            created_at=row.created_at,
        )
    except (ValidationError, ValueError) as exc:
        raise ImageNotFound() from exc


async def get_owned_output(
    session: AsyncSession, principal: Principal, output_id: uuid.UUID
) -> ImageOutput:
    row = await require_downloadable_image_output(session, output_id, principal)
    return output_projection(row)


async def list_owned_outputs(
    session: AsyncSession,
    principal: Principal,
    *,
    limit: int = 25,
    cursor: str | None = None,
    tag: ImageContentTag | None = None,
) -> ImageOutputPage:
    owner_id = principal.user_id
    if owner_id is None:
        raise ImageForbidden()
    if not 1 <= limit <= 100:
        raise ImageValidationError()
    await authorize_live_image_action(session, principal)
    try:
        await authorize_live_image_action(session, principal, explicit=True)
        explicit_allowed = True
    except ImageForbidden:
        explicit_allowed = False
    query = (
        select(ImageOutputRow)
        .where(
            ImageOutputRow.owner_user_id == owner_id,
            ImageOutputRow.state == "published",
            ImageOutputRow.deleted_at.is_(None),
        )
        .order_by(ImageOutputRow.created_at.desc(), ImageOutputRow.id.desc())
        .limit(limit + 1)
    )
    if tag is not None:
        query = query.where(ImageOutputRow.content_tag == tag.value)
    if not explicit_allowed:
        query = query.where(
            ImageOutputRow.content_tag != ImageContentTag.EXPLICIT.value,
            ImageOutputRow.recipe["resolved"]["spec"]["content_mode"].astext == "standard",
        )
    if cursor is not None:
        boundary_at, boundary_id = _decode_cursor(cursor, owner_id)
        query = query.where(
            or_(
                ImageOutputRow.created_at < boundary_at,
                and_(
                    ImageOutputRow.created_at == boundary_at,
                    ImageOutputRow.id < boundary_id,
                ),
            )
        )
    rows = (await session.scalars(query)).all()
    page_rows = rows[:limit]
    return ImageOutputPage(
        items=[
            output_projection(row)
            for row in page_rows
            if output_visible_to_owner(row, owner_id, explicit_allowed=explicit_allowed)
        ],
        next_cursor=_encode_cursor(page_rows[-1]) if len(rows) > limit and page_rows else None,
    )
