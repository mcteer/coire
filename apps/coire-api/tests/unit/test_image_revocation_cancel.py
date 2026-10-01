"""Revoked image authority stops placed work while preserving uncertain holds."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageJobRow
from coire_api.images.quota import _QUOTA_LOCK
from coire_core.errors import ImageConflict
from coire_core.models.images import ImageJobState
from coire_scheduler import image_dispatch, images

JOB = "01J00000000000000000000000"


class Session:
    def __init__(self) -> None:
        self.row = SimpleNamespace(
            id=JOB,
            state=ImageJobState.RUNNING,
            cancel_requested_at=None,
            selected_node_id=uuid.uuid4(),
            instance_id=uuid.uuid4(),
            attempt=1,
            fence=3,
            updated_at=datetime.now(UTC),
            version=1,
        )
        self.calls: list[str] = []

    async def execute(self, statement: object) -> None:
        assert statement is _QUOTA_LOCK
        self.calls.append("quota_lock")

    async def get(self, model: type[object], identity: object, **kwargs: object) -> Any:
        assert model is ImageJobRow and identity == JOB
        assert kwargs == {"populate_existing": True, "with_for_update": True}
        self.calls.append("job_lock")
        return self.row


async def test_revocation_cancel_keeps_lease_and_quota_until_node_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    audits: list[dict[str, object]] = []
    monkeypatch.setattr(image_dispatch, "_policy", lambda _: (None, frozenset(), True))

    async def access(*_: object, **__: object) -> bool:
        return False

    async def audit(*_: object, **kwargs: object) -> None:
        audits.append(kwargs)

    monkeypatch.setattr(image_dispatch, "_access_current", access)
    monkeypatch.setattr(image_dispatch, "write_audit", audit)
    assert await image_dispatch.request_revoked_image_cancel(cast(AsyncSession, session), JOB)
    assert session.calls == ["quota_lock", "job_lock"]
    assert session.row.state == ImageJobState.CANCELLING
    assert session.row.cancel_requested_at is not None
    assert session.row.version == 2
    assert audits[0]["action"] == "image.authorization_revoked"
    assert not await image_dispatch.request_revoked_image_cancel(cast(AsyncSession, session), JOB)
    assert len(audits) == 1


async def test_revocation_recheck_does_not_cancel_restored_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    monkeypatch.setattr(image_dispatch, "_policy", lambda _: (None, frozenset(), False))

    async def access(*_: object, **__: object) -> bool:
        return True

    monkeypatch.setattr(image_dispatch, "_access_current", access)
    assert not await image_dispatch.request_revoked_image_cancel(cast(AsyncSession, session), JOB)
    assert session.row.state == ImageJobState.RUNNING
    assert session.row.cancel_requested_at is None


async def test_fresh_thermal_alarm_audits_fenced_cancel_without_releasing_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    audits: list[dict[str, object]] = []

    async def thermal(*_: object) -> bool:
        return True

    async def audit(*_: object, **kwargs: object) -> None:
        audits.append(kwargs)

    monkeypatch.setattr(image_dispatch, "node_thermal_alarm", thermal)
    monkeypatch.setattr(image_dispatch, "write_audit", audit)
    assert await image_dispatch.request_thermal_image_cancel(cast(AsyncSession, session), JOB)
    assert session.calls == ["quota_lock", "job_lock"]
    assert session.row.state == ImageJobState.CANCELLING
    assert session.row.cancel_requested_at is not None
    assert session.row.version == 2
    assert audits[0]["action"] == "image.thermal_cancel"
    assert not await image_dispatch.request_thermal_image_cancel(cast(AsyncSession, session), JOB)
    assert len(audits) == 1


async def test_recovered_dispatch_cancels_revoked_placed_job_before_worker_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    session.row.state = ImageJobState.RESERVING
    monkeypatch.setattr(image_dispatch, "_policy", lambda _: (None, frozenset(), True))

    async def access(*_: object, **__: object) -> bool:
        return False

    async def audit(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(image_dispatch, "_access_current", access)
    monkeypatch.setattr(image_dispatch, "write_audit", audit)
    assert (
        await image_dispatch.prepare_image_dispatch(
            cast(AsyncSession, session), JOB, cast(Any, object())
        )
        is None
    )
    assert session.row.state == ImageJobState.CANCELLING
    assert session.row.cancel_requested_at is not None


async def test_observation_routes_revoked_job_to_cancel_without_node_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    called: list[str] = []

    @asynccontextmanager
    async def scope() -> Any:
        yield cast(AsyncSession, session)

    async def get(model: type[object], identity: object, **_: object) -> Any:
        assert model is ImageJobRow and identity == JOB
        return session.row

    async def access(*_: object, **__: object) -> bool:
        return False

    async def cancel(_: object, job_id: str) -> bool:
        called.append(job_id)
        return True

    session.get = get  # type: ignore[method-assign]
    monkeypatch.setattr(images, "session_scope", scope)
    monkeypatch.setattr(images, "_policy", lambda _: (None, frozenset(), True))
    monkeypatch.setattr(images, "_access_current", access)
    monkeypatch.setattr(images, "request_revoked_image_cancel", cancel)
    monkeypatch.setattr(images, "NodeClient", lambda _: pytest.fail("must not poll node"))
    assert not await images.observe_image_job(JOB)
    assert called == [JOB]


async def test_generic_failure_cannot_release_a_placed_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    monkeypatch.setattr(
        image_dispatch,
        "_policy",
        lambda _: (SimpleNamespace(resolved=None), frozenset(), False),
    )
    with pytest.raises(ImageConflict, match="termination proof"):
        await image_dispatch.fail_image_attempt(cast(AsyncSession, session), JOB, "attempt_lost")
    assert session.row.state == ImageJobState.RUNNING
    assert session.calls == ["quota_lock", "job_lock"]
