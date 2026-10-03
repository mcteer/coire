"""Image event cursors replay safely and reset after retention gaps."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ImageJobEventRow
from coire_api.images import events
from coire_core.errors import ImageConflict, ImageInvalidCursor
from coire_core.models.images import ImageJob, ImageJobEvent, ImageJobState, ImageSpec

JOB = "01J00000000000000000000000"
OWNER = uuid.uuid4()


def _job(*, latest: int = 4, state: ImageJobState = ImageJobState.QUEUED) -> ImageJob:
    now = datetime.now(UTC)
    return ImageJob(
        id=JOB,
        state=state,
        effective_spec=ImageSpec(
            model_id=uuid.uuid4(),
            prompt="private prompt",
            width=64,
            height=64,
            steps=4,
            guidance=Decimal(0),
            seed=7,
        ),
        resolved=None,
        latest_event_sequence=latest,
        created_at=now,
        updated_at=now,
    )


class _Rows:
    def __init__(self, rows: list[ImageJobEventRow]) -> None:
        self.rows = rows

    def all(self) -> list[ImageJobEventRow]:
        return self.rows


class _Session:
    def __init__(
        self, *, earliest: int | None = 1, rows: list[ImageJobEventRow] | None = None
    ) -> None:
        self.earliest = earliest
        self.rows = rows or []

    async def scalar(self, query: object) -> int | None:
        return self.earliest

    async def scalars(self, query: object) -> _Rows:
        return _Rows(self.rows)


def test_cursor_rejects_other_job_and_noncanonical_sequence() -> None:
    assert events.parse_event_cursor(None, JOB) == 0
    assert events.parse_event_cursor(f"{JOB}:8", JOB) == 8
    for cursor in (f"{JOB}:01", f"{JOB}:0", "garbage", "01J00000000000000000000001:1"):
        with pytest.raises(ImageInvalidCursor):
            events.parse_event_cursor(cursor, JOB)


async def test_stale_cursor_yields_full_snapshot_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    async def owned(*args: object) -> ImageJob:
        return _job()

    monkeypatch.setattr(events, "get_owned_image_job", owned)
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    snapshot, pending = await events.read_owned_image_events(
        cast(AsyncSession, _Session(earliest=3)), principal, JOB, 1
    )
    assert snapshot.latest_event_sequence == 4
    assert len(pending) == 1
    assert pending[0].type == "reset"
    assert pending[0].snapshot == snapshot
    assert events.event_cursor(pending[0]) == f"{JOB}:4"
    assert "private prompt" in events.encode_event(pending[0])


async def test_fully_pruned_history_resets_and_rejects_future_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def owned(*args: object) -> ImageJob:
        return _job(latest=4)

    monkeypatch.setattr(events, "get_owned_image_job", owned)
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    session = cast(AsyncSession, _Session(earliest=None))
    _, replay = await events.read_owned_image_events(session, principal, JOB, 1)
    assert len(replay) == 1
    assert replay[0].type == "reset"
    assert replay[0].sequence == 4
    _, current = await events.read_owned_image_events(session, principal, JOB, 4)
    assert current == []
    with pytest.raises(ImageInvalidCursor):
        await events.read_owned_image_events(session, principal, JOB, 5)


async def test_replay_rejects_corrupt_persisted_event(monkeypatch: pytest.MonkeyPatch) -> None:
    async def owned(*args: object) -> ImageJob:
        return _job(latest=1)

    monkeypatch.setattr(events, "get_owned_image_job", owned)
    event = ImageJobEvent(
        job_id=JOB, sequence=1, at=datetime.now(UTC), type="queued", state=ImageJobState.QUEUED
    )
    row = ImageJobEventRow(
        job_id=JOB, sequence=1, event_type="queued", payload=event.model_dump(mode="json")
    )
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    _, pending = await events.read_owned_image_events(
        cast(AsyncSession, _Session(rows=[row])), principal, JOB, 0
    )
    assert pending == [event]
    gap = ImageJobEventRow(
        job_id=JOB, sequence=2, event_type="queued", payload=event.model_dump(mode="json")
    )
    _, reset = await events.read_owned_image_events(
        cast(AsyncSession, _Session(rows=[gap])), principal, JOB, 0
    )
    assert reset[0].type == "reset"
    row.event_type = "done"
    with pytest.raises(ImageConflict):
        await events.read_owned_image_events(
            cast(AsyncSession, _Session(rows=[row])), principal, JOB, 0
        )
