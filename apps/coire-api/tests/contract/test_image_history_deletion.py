"""History deletion is owner-scoped, terminal-only, and preserves library provenance."""

import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.images import history
from coire_core.errors import ImageConflict, ImageNotFound
from coire_core.models.images import ImageHistoryDeletionRequest

JOB = "01J00000000000000000000000"
OTHER = "01J00000000000000000000001"


@pytest.mark.parametrize("states,missing", [(["running"], False), (["succeeded"], True)])
async def test_batch_validates_every_row_before_deleting(
    states: list[str], missing: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = uuid.uuid4()
    auth = AsyncMock(return_value=owner)
    monkeypatch.setattr(history, "authorize_live_image_action", auth)
    session = AsyncMock(spec=AsyncSession)
    rows = [SimpleNamespace(id=JOB, state=states[0], history_deleted_at=None)]
    session.scalars.return_value = SimpleNamespace(all=lambda: rows)
    request = ImageHistoryDeletionRequest(job_ids=[JOB, OTHER] if missing else [JOB])
    with pytest.raises(ImageNotFound if missing else ImageConflict):
        await history.delete_owned_history(
            cast(AsyncSession, session), Principal(kind=PrincipalKind.USER, user_id=owner), request
        )
    session.execute.assert_not_awaited()
    assert rows[0].history_deleted_at is None
    query = str(session.scalars.call_args.args[0])
    assert "image_jobs.owner_user_id" in query
    assert "FOR UPDATE" in query


async def test_event_rows_are_permanently_deleted_without_deleting_output_or_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid.uuid4()
    monkeypatch.setattr(history, "authorize_live_image_action", AsyncMock(return_value=owner))
    audit = AsyncMock()
    monkeypatch.setattr(history, "write_audit", audit)
    rows = [SimpleNamespace(id=JOB, state="succeeded", history_deleted_at=None)]
    session = AsyncMock(spec=AsyncSession)
    session.scalars.return_value = SimpleNamespace(all=lambda: rows)
    receipt = await history.delete_owned_history(
        cast(Any, session),
        Principal(kind=PrincipalKind.USER, user_id=owner),
        ImageHistoryDeletionRequest(job_ids=[JOB]),
    )
    assert receipt.deleted_job_ids == [JOB]
    sql = str(session.execute.call_args.args[0])
    assert sql.startswith("DELETE FROM image_job_events")
    assert "image_outputs" not in sql
    assert rows[0].history_deleted_at is not None
    assert audit.call_args.kwargs["action"] == "image.history.deleted"
    session.commit.assert_not_awaited()  # route commits mutation and audit atomically
