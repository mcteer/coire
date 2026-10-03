"""Owner-only image job snapshots with live entitlement checks."""

from __future__ import annotations

import base64
import binascii
import re
import struct
import uuid
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import ImageJobEventRow, ImageJobRow, ImageOutputRow
from coire_api.images.authorization import (
    _output_requires_explicit,
    authorize_live_image_action,
    output_visible_to_owner,
    require_owned_image_job,
)
from coire_api.images.outputs import output_projection
from coire_core.errors import ImageConflict, ImageForbidden, ImageNotFound, ImageValidationError
from coire_core.models.files import ULID_PATTERN
from coire_core.models.images import (
    ImageContentMode,
    ImageJob,
    ImageJobPage,
    ImageJobSettingsSnapshot,
    ImageJobState,
)

_JOB_ID = re.compile(ULID_PATTERN)
_CURSOR_VERSION = 1
_CURSOR_BYTES = 51
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _encode_cursor(row: ImageJobRow) -> str:
    elapsed = row.created_at.astimezone(UTC) - _EPOCH
    micros = elapsed.days * 86_400_000_000 + elapsed.seconds * 1_000_000 + elapsed.microseconds
    payload = (
        struct.pack(">BQ", _CURSOR_VERSION, micros)
        + row.owner_user_id.bytes
        + row.id.encode("ascii")
    )
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str, owner_id: uuid.UUID) -> tuple[datetime, str]:
    try:
        if len(cursor) > 100:
            raise ValueError("cursor too long")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if len(raw) != _CURSOR_BYTES or raw[0] != _CURSOR_VERSION:
            raise ValueError("invalid cursor version or size")
        _, micros = struct.unpack(">BQ", raw[:9])
        if uuid.UUID(bytes=raw[9:25]) != owner_id:
            raise ValueError("cursor owner differs")
        job_id = raw[25:].decode("ascii")
        if _JOB_ID.fullmatch(job_id) is None:
            raise ValueError("cursor job identity is invalid")
        return _EPOCH + timedelta(microseconds=micros), job_id
    except (ValueError, OverflowError, UnicodeDecodeError, binascii.Error) as exc:
        raise ImageNotFound() from exc


def _policy(row: ImageJobRow) -> tuple[ImageJobSettingsSnapshot, frozenset[str], bool]:
    try:
        snapshot = ImageJobSettingsSnapshot.model_validate(row.resolved_spec)
        policy = row.authorization_snapshot
        if not isinstance(policy, dict):
            raise ValueError("missing image policy")
        values = policy.get("required_entitlements")
        if (
            not isinstance(values, list)
            or len(values) > 32
            or any(not isinstance(value, str) or not value or len(value) > 64 for value in values)
        ):
            raise ValueError("invalid image policy")
        explicit = policy.get("explicit")
        if not isinstance(explicit, bool):
            raise ValueError("invalid image content policy")
        return (
            snapshot,
            frozenset(values),
            explicit or snapshot.effective_spec.content_mode is ImageContentMode.EXPLICIT,
        )
    except (ValidationError, ValueError):
        raise ImageConflict("image job settings unavailable") from None


async def get_owned_image_job(session: AsyncSession, principal: Principal, job_id: str) -> ImageJob:
    row = await require_owned_image_job(session, job_id, principal)
    snapshot, required, explicit = _policy(row)
    await authorize_live_image_action(
        session, principal, explicit=explicit, required_entitlements=required
    )
    latest = await session.scalar(
        select(func.max(ImageJobEventRow.sequence)).where(ImageJobEventRow.job_id == job_id)
    )
    published = (
        await session.scalars(
            select(ImageOutputRow)
            .where(
                ImageOutputRow.job_id == job_id,
                ImageOutputRow.owner_user_id == row.owner_user_id,
                ImageOutputRow.state == "published",
                ImageOutputRow.deleted_at.is_(None),
            )
            .order_by(ImageOutputRow.output_index)
        )
    ).all()
    visible = []
    explicit_output_access: bool | None = True if explicit else None
    for item in published:
        try:
            requires_explicit = _output_requires_explicit(item)
        except ImageForbidden:
            continue
        if requires_explicit and explicit_output_access is None:
            try:
                await authorize_live_image_action(session, principal, explicit=True)
                explicit_output_access = True
            except ImageForbidden:
                explicit_output_access = False
        if output_visible_to_owner(
            item, row.owner_user_id, explicit_allowed=bool(explicit_output_access)
        ):
            visible.append(output_projection(item))
    try:
        return ImageJob(
            id=row.id,
            state=ImageJobState(row.state),
            effective_spec=snapshot.effective_spec,
            resolved=snapshot.resolved,
            failure_code=row.safe_failure_code,
            latest_event_sequence=latest or 0,
            outputs=visible,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
    except (ValidationError, ValueError):
        raise ImageConflict("image job state unavailable") from None


async def list_owned_image_jobs(
    session: AsyncSession,
    principal: Principal,
    *,
    limit: int = 25,
    cursor: str | None = None,
    state: ImageJobState | None = None,
) -> ImageJobPage:
    """Scan a bounded owner page, omitting jobs whose live policy is no longer readable."""
    if not 1 <= limit <= 100:
        raise ImageValidationError("invalid image page size")
    owner_id = await authorize_live_image_action(session, principal)
    query = select(ImageJobRow).where(ImageJobRow.owner_user_id == owner_id)
    if state is not None:
        query = query.where(ImageJobRow.state == state.value)
    if cursor is not None:
        boundary_at, boundary_id = _decode_cursor(cursor, owner_id)
        query = query.where(
            or_(
                ImageJobRow.created_at < boundary_at,
                and_(ImageJobRow.created_at == boundary_at, ImageJobRow.id < boundary_id),
            )
        )
    rows = (
        await session.scalars(
            query.order_by(ImageJobRow.created_at.desc(), ImageJobRow.id.desc()).limit(101)
        )
    ).all()
    items: list[ImageJob] = []
    last_examined: ImageJobRow | None = None
    consumed = 0
    for row in rows[:100]:
        consumed += 1
        last_examined = row
        try:
            items.append(await get_owned_image_job(session, principal, row.id))
        except (ImageForbidden, ImageNotFound):
            continue
        if len(items) >= limit:
            break
    return ImageJobPage(
        items=items,
        next_cursor=_encode_cursor(last_examined)
        if last_examined is not None and consumed < len(rows)
        else None,
    )
