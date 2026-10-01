"""Owner-scoped image event replay with a durable reset after retention gaps."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import ImageJobEventRow
from coire_api.images.jobs import get_owned_image_job
from coire_core.errors import ImageConflict, ImageInvalidCursor
from coire_core.models.images import ImageJob, ImageJobEvent, ImageJobState

_CURSOR = re.compile(r"(?P<job>[0-9A-HJKMNP-TV-Z]{26}):(?P<sequence>[1-9][0-9]{0,17})\Z")
_TERMINAL = frozenset({ImageJobState.SUCCEEDED, ImageJobState.FAILED, ImageJobState.CANCELLED})


def parse_event_cursor(value: str | None, job_id: str) -> int:
    if value is None:
        return 0
    match = _CURSOR.fullmatch(value)
    if match is None or match.group("job") != job_id:
        raise ImageInvalidCursor()
    return int(match.group("sequence"))


def event_cursor(event: ImageJobEvent) -> str:
    return f"{event.job_id}:{event.sequence}"


def encode_event(event: ImageJobEvent) -> str:
    return f"id: {event_cursor(event)}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"


def _reset_event(job: ImageJob) -> ImageJobEvent:
    return ImageJobEvent(
        job_id=job.id,
        sequence=job.latest_event_sequence,
        at=datetime.now(UTC),
        type="reset",
        state=job.state,
        snapshot=job,
    )


async def read_owned_image_events(
    session: AsyncSession,
    principal: Principal,
    job_id: str,
    after_sequence: int,
    *,
    limit: int = 100,
) -> tuple[ImageJob, list[ImageJobEvent]]:
    """Recheck live policy before each bounded read; never expose another owner's rows."""
    if after_sequence < 0 or not 1 <= limit <= 100:
        raise ImageInvalidCursor()
    job = await get_owned_image_job(session, principal, job_id)
    if after_sequence > job.latest_event_sequence:
        raise ImageInvalidCursor()
    earliest = await session.scalar(
        select(func.min(ImageJobEventRow.sequence)).where(ImageJobEventRow.job_id == job_id)
    )
    if earliest is None:
        if after_sequence < job.latest_event_sequence:
            return job, [_reset_event(job)]
        return job, []
    if earliest > job.latest_event_sequence:
        raise ImageConflict("image event history differs from job cursor")
    if after_sequence < earliest - 1:
        return job, [_reset_event(job)]
    rows = (
        await session.scalars(
            select(ImageJobEventRow)
            .where(
                ImageJobEventRow.job_id == job_id,
                ImageJobEventRow.sequence > after_sequence,
                ImageJobEventRow.sequence <= job.latest_event_sequence,
            )
            .order_by(ImageJobEventRow.sequence)
            .limit(limit)
        )
    ).all()
    events: list[ImageJobEvent] = []
    expected_sequence = after_sequence + 1
    for row in rows:
        if row.sequence != expected_sequence:
            return job, [_reset_event(job)]
        try:
            event = ImageJobEvent.model_validate(row.payload)
        except ValidationError as exc:
            raise ImageConflict("image event history is invalid") from exc
        if event.job_id != job_id or event.sequence != row.sequence or event.type != row.event_type:
            raise ImageConflict("image event history differs from persisted identity")
        events.append(event)
        expected_sequence += 1
    if not events and after_sequence < job.latest_event_sequence:
        return job, [_reset_event(job)]
    return job, events


def is_terminal(job: ImageJob) -> bool:
    return job.state in _TERMINAL
