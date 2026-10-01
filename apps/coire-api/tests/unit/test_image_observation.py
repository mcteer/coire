"""Node journal observations must be fenced, monotonic and bounded in rate."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageJobEventRow, ImageJobRow
from coire_api.images.observation import reconcile_image_observation
from coire_api.nodes_client import NodeError, NodeErrorKind
from coire_core.errors import ImageConflict
from coire_core.models.image_worker import NodeImageJob
from coire_scheduler import images

JOB = "01J00000000000000000000000"
NODE = "coire-edge-b"


class Session:
    def __init__(self) -> None:
        self.node_id = uuid.uuid4()
        self.instance_id = uuid.uuid4()
        self.row = ImageJobRow(
            id=JOB,
            state="reserving",
            version=2,
            attempt=1,
            fence=3,
            selected_node_id=self.node_id,
            instance_id=self.instance_id,
            progress=0.0,
            cancel_requested_at=None,
        )
        self.latest = ImageJobEventRow(
            job_id=JOB,
            sequence=1,
            event_type="queued",
            payload={},
            created_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        self.lease: object | None = object()
        self.added: list[ImageJobEventRow] = []

    async def get(self, model: type[object], identity: object, **_: object) -> Any:
        assert model is ImageJobRow and identity == JOB
        return self.row

    async def scalar(self, statement: object) -> Any:
        assert statement is not None
        self.queries = getattr(self, "queries", 0) + 1
        return self.lease if self.queries % 2 else self.latest

    def add(self, event: ImageJobEventRow) -> None:
        self.added.append(event)
        self.latest = event


def status(session: Session, *, state: str = "running", step: int = 1) -> NodeImageJob:
    return NodeImageJob.model_validate(
        {
            "job_id": JOB,
            "attempt": 1,
            "fence": 3,
            "node": NODE,
            "instance_id": str(session.instance_id),
            "state": state,
            "progress_step": step if state == "running" else None,
            "progress_total": 10 if state == "running" else None,
            "updated_at": datetime.now(UTC),
        }
    )


async def test_observation_starts_once_and_rate_limits_progress() -> None:
    session = Session()

    async def reconcile(observed: NodeImageJob) -> bool:
        return await reconcile_image_observation(
            cast(AsyncSession, session), JOB, session.node_id, observed, expected_node=NODE
        )

    assert await reconcile(status(session))
    assert session.row.state == "running"
    assert session.row.progress == 0.1
    assert [item.event_type for item in session.added] == ["started"]
    assert await reconcile(status(session, step=2))
    assert session.row.progress == 0.2
    assert len(session.added) == 1
    session.latest.created_at -= timedelta(seconds=1)
    assert await reconcile(status(session, step=3))
    assert [item.event_type for item in session.added] == ["started", "progress"]
    assert await reconcile(status(session, step=10))
    version_at_cap = session.row.version
    assert session.row.progress == 0.99
    assert await reconcile(status(session, step=10))
    assert session.row.version == version_at_cap
    assert not await reconcile(status(session, state="transferring"))
    assert session.row.state == "transferring"
    assert session.row.progress == 0.99


async def test_observation_refuses_different_attempt_or_missing_lease() -> None:
    session = Session()
    observed = status(session)
    wrong = observed.model_copy(update={"fence": 4})
    with pytest.raises(ImageConflict, match="attempt differs"):
        await reconcile_image_observation(
            cast(AsyncSession, session), JOB, session.node_id, wrong, expected_node=NODE
        )
    session.lease = None
    with pytest.raises(ImageConflict, match="lease is unavailable"):
        await reconcile_image_observation(
            cast(AsyncSession, session), JOB, session.node_id, observed, expected_node=NODE
        )
    assert session.row.state == "reserving"


async def test_scheduler_observes_existing_attempt_without_starting_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    observed = status(session, state="transferring")
    calls: list[str] = []

    class Node:
        name = NODE

    async def get(model: type[object], identity: object, **_: object) -> Any:
        if model is ImageJobRow:
            return session.row
        assert identity == session.node_id
        return Node()

    session.get = get  # type: ignore[method-assign]

    @asynccontextmanager
    async def scope() -> Any:
        yield cast(AsyncSession, session)

    class Client:
        def __init__(self, _: object) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def image_job_status(self, node: str, binding: object) -> NodeImageJob:
            calls.append(node)
            assert binding == images._binding(session.row)
            return observed

        async def start_image_job(self, *_: object) -> None:
            raise AssertionError("restart observation must not start generation")

    monkeypatch.setattr(images, "session_scope", scope)
    monkeypatch.setattr(images, "NodeClient", Client)
    monkeypatch.setattr(images, "get_settings", lambda: object())
    monkeypatch.setattr(images, "_policy", lambda _: (None, frozenset(), False))

    async def access(*_: object, **__: object) -> bool:
        return True

    monkeypatch.setattr(images, "_access_current", access)

    async def no_thermal(*_: object) -> bool:
        return False

    monkeypatch.setattr(images, "request_thermal_image_cancel", no_thermal)
    assert not await images.observe_image_job(JOB)
    assert calls == [NODE]
    assert session.row.state == "transferring"


async def test_missing_node_journal_keeps_the_placed_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()

    class Node:
        name = NODE

    async def get(model: type[object], identity: object, **_: object) -> Any:
        if model is ImageJobRow:
            return session.row
        assert identity == session.node_id
        return Node()

    @asynccontextmanager
    async def scope() -> Any:
        yield cast(AsyncSession, session)

    class Client:
        def __init__(self, _: object) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def image_job_status(self, *_: object) -> NodeImageJob:
            raise NodeError(NodeErrorKind.NOT_FOUND, NODE, status=404)

    async def access(*_: object, **__: object) -> bool:
        return True

    monkeypatch.setattr(session, "get", get)
    monkeypatch.setattr(images, "session_scope", scope)
    monkeypatch.setattr(images, "NodeClient", Client)
    monkeypatch.setattr(images, "_policy", lambda _: (None, frozenset(), False))
    monkeypatch.setattr(images, "_access_current", access)

    async def no_thermal(*_: object) -> bool:
        return False

    monkeypatch.setattr(images, "request_thermal_image_cancel", no_thermal)
    monkeypatch.setattr(images, "get_settings", lambda: object())
    with pytest.raises(ImageConflict, match="journal is unavailable"):
        await images.observe_image_job(JOB)
    assert session.row.state == "reserving"
    assert session.row.fence == 3


async def test_live_thermal_alarm_requests_stop_without_observing_or_restarting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    calls: list[str] = []

    @asynccontextmanager
    async def scope() -> Any:
        yield cast(AsyncSession, session)

    async def access(*_: object, **__: object) -> bool:
        return True

    async def thermal(_: object, job_id: str) -> bool:
        calls.append(job_id)
        session.row.state = "cancelling"
        return True

    monkeypatch.setattr(images, "session_scope", scope)
    monkeypatch.setattr(images, "_policy", lambda _: (None, frozenset(), False))
    monkeypatch.setattr(images, "_access_current", access)
    monkeypatch.setattr(images, "request_thermal_image_cancel", thermal)
    assert not await images.observe_image_job(JOB)
    assert calls == [JOB]
    assert session.row.state == "cancelling"
