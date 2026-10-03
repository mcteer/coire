"""Queue deadlines release capacity only when execution never reached a node."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageJobEventRow, ImageJobRow
from coire_api.images import expiry
from coire_core.errors import ImageConflict
from coire_core.models.images import ImageJobSettingsSnapshot, ImageSpec

JOB = "01J00000000000000000000000"
OWNER = uuid.uuid4()
NOW = datetime.now(UTC)


def _row() -> Any:
    spec = ImageSpec(
        model_id=uuid.uuid4(),
        prompt="private subject",
        width=512,
        height=512,
        steps=9,
        guidance=Decimal(0),
        seed=7,
    )
    return SimpleNamespace(
        id=JOB,
        owner_user_id=OWNER,
        state="queued",
        deadline_at=NOW - timedelta(seconds=1),
        fence=0,
        selected_node_id=None,
        instance_id=None,
        reservation_ids=[],
        cancel_requested_at=None,
        resolved_spec=ImageJobSettingsSnapshot(effective_spec=spec).model_dump(mode="json"),
        authorization_snapshot={
            "required_entitlements": [],
            "explicit": False,
            "output_hold_bytes": 64,
        },
        version=1,
        cleanup_state="pending",
        receipt_state="pending",
        safe_failure_code=None,
        updated_at=NOW,
        finished_at=None,
    )


class Session:
    def __init__(self, row: Any, *, active_lease: bool = False) -> None:
        self.row = row
        self.active_lease = active_lease
        self.scalar_calls = 0
        self.calls: list[str] = []
        self.added: list[object] = []

    async def execute(self, statement: object) -> None:
        self.calls.append("quota_lock")

    async def get(self, model: type[object], identity: object, **kwargs: object) -> Any:
        assert model is ImageJobRow and identity == JOB
        assert kwargs == {"populate_existing": True, "with_for_update": True}
        self.calls.append("job_lock")
        return self.row

    async def scalar(self, statement: object) -> int | None:
        self.calls.append("scalar")
        self.scalar_calls += 1
        return (1 if self.active_lease else None) if self.scalar_calls == 1 else 1

    def add(self, value: object) -> None:
        self.added.append(value)


async def test_expired_unplaced_job_releases_hold_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row()
    session = Session(row)

    async def release(db: object, owner: uuid.UUID, count: int, held: int) -> None:
        assert db is session and (owner, count, held) == (OWNER, 1, 64)
        session.calls.append("release")

    async def audit(db: object, **kwargs: object) -> None:
        assert db is session and kwargs["action"] == "image.queue.expired"
        assert "private subject" not in str(kwargs)
        session.calls.append("audit")

    monkeypatch.setattr(expiry, "release_pending_image_job_capacity", release)
    monkeypatch.setattr(expiry, "write_audit", audit)
    assert await expiry.expire_queued_image_job(cast(AsyncSession, session), JOB, now=NOW)
    assert session.calls == ["quota_lock", "job_lock", "scalar", "scalar", "release", "audit"]
    assert row.state == "failed" and row.safe_failure_code == "queue_timeout"
    assert row.cleanup_state == "cleaned" and row.receipt_state == "none"
    assert row.finished_at == NOW and row.version == 2
    event = cast(ImageJobEventRow, session.added[0])
    assert event.sequence == 2 and event.event_type == "error"
    assert event.payload["safe_code"] == "queue_timeout"
    assert "private subject" not in str(event.payload)
    assert not await expiry.expire_queued_image_job(cast(AsyncSession, session), JOB, now=NOW)
    assert session.calls.count("release") == 1


@pytest.mark.parametrize("change", ["future", "fenced", "selected", "reserved", "cancelled"])
async def test_expiry_retains_uncertain_or_active_holds(change: str) -> None:
    row = _row()
    if change == "future":
        row.deadline_at = NOW + timedelta(seconds=1)
    elif change == "fenced":
        row.fence = 1
    elif change == "selected":
        row.selected_node_id = uuid.uuid4()
    elif change == "reserved":
        row.reservation_ids = ["reservation"]
    else:
        row.cancel_requested_at = NOW
    session = Session(row)
    if change == "future":
        assert not await expiry.expire_queued_image_job(cast(AsyncSession, session), JOB, now=NOW)
    else:
        with pytest.raises(ImageConflict, match="uncertain placement"):
            await expiry.expire_queued_image_job(cast(AsyncSession, session), JOB, now=NOW)
    assert row.state == "queued" and session.added == []


async def test_expiry_retains_active_lease_even_without_placement_fields() -> None:
    session = Session(_row(), active_lease=True)
    with pytest.raises(ImageConflict, match="active execution lease"):
        await expiry.expire_queued_image_job(cast(AsyncSession, session), JOB, now=NOW)
    assert session.row.state == "queued" and session.added == []
