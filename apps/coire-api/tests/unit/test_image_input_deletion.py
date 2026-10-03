"""Owner input tombstones hide bytes and preserve active job references."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ImageInputRow
from coire_api.images import input_deletion
from coire_core.errors import ImageConflict, ImageNotFound

OWNER = uuid.uuid4()


def _row() -> ImageInputRow:
    input_id = uuid.uuid4()
    return ImageInputRow(
        id=input_id,
        owner_user_id=OWNER,
        purpose="recipe",
        original_key=str(input_id),
        original_bytes=7,
        original_sha256="a" * 64,
        state="ready",
        held_bytes=0,
        active_references=0,
        created_at=datetime.now(UTC),
    )


class FakeSession:
    def __init__(self, row: ImageInputRow) -> None:
        self.row = row
        self.job_ids: list[str] = []
        self.added: list[object] = []

    async def execute(self, statement: object) -> None:
        assert statement is not None

    async def scalars(self, statement: object) -> SimpleNamespace:
        query = str(statement.compile(compile_kwargs={"literal_binds": True}))  # type: ignore[attr-defined]
        assert "'transferring'" in query
        assert "image_jobs.owner_user_id" in query
        assert "image_jobs.resolved_spec" in query
        return SimpleNamespace(all=lambda: self.job_ids)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        pass

    async def get(self, model: type[object], identity: object, **kwargs: object) -> ImageInputRow:
        assert model is ImageInputRow and identity == self.row.id
        assert kwargs.get("with_for_update") is True
        return self.row


def test_owner_tombstone_is_repeatable_and_purged_is_repeatable() -> None:
    row = _row()
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    session = FakeSession(row)
    first = asyncio.run(input_deletion.tombstone_owned_input(session, principal, row.id))  # type: ignore[arg-type]
    assert first.state == "deleting" and row.deleted_at is not None
    second = asyncio.run(input_deletion.tombstone_owned_input(session, principal, row.id))  # type: ignore[arg-type]
    assert second.state == "deleting" and second.id == first.id
    row.state = "purged"
    row.purged_at = datetime.now(UTC)
    third = asyncio.run(input_deletion.tombstone_owned_input(session, principal, row.id))  # type: ignore[arg-type]
    assert third.state == "purged"


def test_other_owner_and_live_references_are_refused_without_mutation() -> None:
    row = _row()
    session = FakeSession(row)
    other = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    with pytest.raises(ImageNotFound):
        asyncio.run(input_deletion.tombstone_owned_input(session, other, row.id))  # type: ignore[arg-type]
    row.active_references = 1
    owner = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    with pytest.raises(ImageConflict):
        asyncio.run(input_deletion.tombstone_owned_input(session, owner, row.id))  # type: ignore[arg-type]
    assert row.state == "ready" and row.deleted_at is None


async def test_deleting_referenced_input_requests_job_cancellation_before_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row()
    row.active_references = 1
    session = FakeSession(row)
    session.job_ids = ["01J00000000000000000000000"]
    owner = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    calls: list[str] = []

    async def cancel(
        db: object, principal: object, job_id: str, *, commit: bool
    ) -> tuple[object, bool]:
        assert db is session and principal is owner and commit is False
        assert row.deleted_at is None
        calls.append(job_id)
        return object(), False

    monkeypatch.setattr(input_deletion, "request_image_job_cancel_for_input_deletion", cancel)
    result = await input_deletion.tombstone_owned_input(session, owner, row.id)  # type: ignore[arg-type]
    assert result.state == "deleting" and row.deleted_at is not None
    assert row.active_references == 1 and calls == session.job_ids
    assert any(getattr(item, "action", None) == "image.input.delete" for item in session.added)
