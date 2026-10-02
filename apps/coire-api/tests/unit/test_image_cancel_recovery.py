"""Scheduler terminal cancellation requires exact node, lease and core cleanup."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageJobEventRow, ImageJobRow
from coire_core.errors import ImageConflict
from coire_core.models.image_worker import NodeImageJob
from coire_core.models.images import ImageJobSettingsSnapshot, ImageSpec
from coire_core.settings import Settings
from coire_scheduler import images

JOB = "01J00000000000000000000000"
NODE = "coire-edge-b"
NODE_ID = uuid.uuid4()
OWNER = uuid.uuid4()
INSTANCE = uuid.uuid4()


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
        state="cancelling",
        cancel_requested_at=datetime.now(UTC),
        selected_node_id=NODE_ID,
        instance_id=INSTANCE,
        attempt=1,
        fence=3,
        resolved_spec=ImageJobSettingsSnapshot(effective_spec=spec).model_dump(mode="json"),
        authorization_snapshot={
            "required_entitlements": [],
            "explicit": False,
            "output_hold_bytes": 64,
        },
        cleanup_state="pending",
        receipt_state="pending",
        version=1,
        updated_at=datetime.now(UTC),
        finished_at=None,
    )


def _status(**updates: object) -> NodeImageJob:
    values: dict[str, object] = {
        "job_id": JOB,
        "attempt": 1,
        "fence": 3,
        "node": NODE,
        "instance_id": INSTANCE,
        "state": "cancelled",
        "scratch_cleaned": True,
        "updated_at": datetime.now(UTC),
    }
    values.update(updates)
    if values["state"] == "succeeded":
        values["receipts"] = (
            {
                "job_id": JOB,
                "attempt": values["attempt"],
                "fence": values["fence"],
                "node": NODE,
                "index": 0,
                "output_id": uuid.uuid4(),
                "byte_count": 7,
                "sha256": "a" * 64,
                "recipe_sha256": "b" * 64,
                "verified_at": datetime.now(UTC),
            },
        )
    return NodeImageJob.model_validate(values)


class Session:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.lease = SimpleNamespace(
            mode="image",
            node_id=NODE_ID,
            fence=3,
            released_at=None,
            release_evidence=None,
        )
        self.transfer = SimpleNamespace(state="received", lease_expires_at=datetime.now(UTC))
        self.calls: list[str] = []
        self.added: list[object] = []
        self.scalar_calls = 0
        self.scalars_calls = 0

    async def execute(self, statement: object) -> None:
        self.calls.append("quota_lock")

    async def get(self, model: type[object], identity: object, **kwargs: object) -> Any:
        assert model is ImageJobRow and identity == JOB
        assert kwargs == {"populate_existing": True, "with_for_update": True}
        self.calls.append("job_lock")
        return self.row

    async def scalar(self, statement: object) -> int | None:
        self.scalar_calls += 1
        return None if self.scalar_calls == 1 else 1

    async def scalars(self, statement: object) -> Any:
        self.scalars_calls += 1
        rows = [self.lease] if self.scalars_calls == 1 else [self.transfer]
        return SimpleNamespace(all=lambda: rows)

    def add(self, value: object) -> None:
        self.added.append(value)


@pytest.mark.parametrize("node_state", ["cancelled", "failed", "succeeded"])
async def test_cancel_finalizes_only_after_staging_and_node_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, node_state: str
) -> None:
    row = _row()
    row.attempt = 2
    session = Session(row)
    root = tmp_path / "blobs"
    earlier = root / "image-staging" / JOB / "1"
    attempt = root / "image-staging" / JOB / "2"
    attempt.mkdir(parents=True)
    earlier.mkdir()
    for directory in (root, root / "image-staging", root / "image-staging" / JOB, attempt, earlier):
        directory.chmod(0o700)
    output = attempt / "0.png"
    output.write_bytes(b"private partial output")
    output.chmod(0o600)
    old_output = earlier / "0.png"
    old_output.write_bytes(b"older partial output")
    old_output.chmod(0o600)
    calls: list[str] = []

    async def release(db: object, owner: uuid.UUID, count: int, held: int) -> None:
        calls.append("release")
        assert (owner, count, held) == (OWNER, 1, 64)
        assert not output.exists() and not old_output.exists()

    monkeypatch.setattr(images, "release_pending_image_job_capacity", release)
    settings = Settings(_secrets_dir="/nonexistent", image_blob_root=str(root))  # type: ignore[call-arg]
    changed = await images.finalize_cancelled_image_job(
        cast(AsyncSession, session), _status(attempt=2, state=node_state), NODE_ID, settings
    )
    assert changed and row.state == "cancelled"
    assert row.cleanup_state == "cleaned" and row.finished_at is not None
    assert calls == ["release"]
    assert not attempt.exists() and not earlier.exists()
    assert session.calls == ["quota_lock", "job_lock"]
    assert session.lease.released_at is not None
    assert session.lease.release_evidence["scratch_cleaned"] is True
    assert session.lease.release_evidence["node_state"] == node_state
    assert session.transfer.state == "cancelled"
    assert len(session.added) == 1
    event = cast(ImageJobEventRow, session.added[0])
    assert event.event_type == "cancelled" and event.sequence == 2
    assert "private subject" not in str(event.payload)


async def test_uncertain_node_or_lease_retains_core_staging(tmp_path: Path) -> None:
    row = _row()
    session = Session(row)
    settings = Settings(_secrets_dir="/nonexistent", image_blob_root=str(tmp_path))  # type: ignore[call-arg]
    with pytest.raises(ImageConflict):
        await images.finalize_cancelled_image_job(
            cast(AsyncSession, session), _status(scratch_cleaned=False), NODE_ID, settings
        )
    assert session.calls == [] and row.state == "cancelling"
    session.lease.fence = 4
    with pytest.raises(ImageConflict):
        await images.finalize_cancelled_image_job(
            cast(AsyncSession, session), _status(), NODE_ID, settings
        )
    assert row.state == "cancelling" and session.added == []


async def test_failed_worker_releases_hold_only_after_exact_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _row()
    row.state = "running"
    row.cancel_requested_at = None
    row.attempt = 2
    session = Session(row)
    root = tmp_path / "blobs"
    earlier = root / "image-staging" / JOB / "1"
    attempt = root / "image-staging" / JOB / "2"
    attempt.mkdir(parents=True)
    earlier.mkdir()
    for directory in (root, root / "image-staging", root / "image-staging" / JOB, attempt, earlier):
        directory.chmod(0o700)
    output = attempt / "0.png"
    output.write_bytes(b"private partial output")
    output.chmod(0o600)
    old_output = earlier / "0.png"
    old_output.write_bytes(b"older partial output")
    old_output.chmod(0o600)
    released: list[int] = []
    audits: list[str] = []

    async def release(db: object, owner: uuid.UUID, count: int, held: int) -> None:
        assert db is session and owner == OWNER and count == 1
        assert not output.exists() and not old_output.exists()
        released.append(held)

    async def audit(db: object, **kwargs: object) -> None:
        assert db is session
        audits.append(cast(str, kwargs["action"]))

    monkeypatch.setattr(images, "release_pending_image_job_capacity", release)
    monkeypatch.setattr(images, "write_audit", audit)
    settings = Settings(_secrets_dir="/nonexistent", image_blob_root=str(root))  # type: ignore[call-arg]
    failed = _status(state="failed", attempt=2)
    with pytest.raises(ImageConflict, match="cleanup is incomplete"):
        await images.finalize_failed_image_job(
            cast(AsyncSession, session),
            failed.model_copy(update={"scratch_cleaned": False}),
            NODE_ID,
            settings,
        )
    assert row.state == "running" and released == []
    assert await images.finalize_failed_image_job(
        cast(AsyncSession, session), failed, NODE_ID, settings
    )
    assert row.state == "failed" and row.cleanup_state == "cleaned"
    assert not earlier.exists() and not attempt.exists()
    assert row.safe_failure_code == "generation_failed" and row.finished_at is not None
    assert released == [64] and audits == ["image.attempt.failed"]
    assert session.lease.released_at is not None
    event = cast(ImageJobEventRow, session.added[0])
    assert event.event_type == "error" and "private subject" not in str(event.payload)
