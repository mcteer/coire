"""Admin worker unload drains placement before a node command and retains uncertainty."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ModelInstanceRow, NodeRow
from coire_api.nodes_client import NodeError, NodeErrorKind
from coire_api.routes import admin_images
from coire_core.errors import ImageConflict
from coire_core.models.image_worker import ImageWorkerLoadResult
from coire_core.models.instance import InstanceState
from coire_core.models.placement import MemoryReservationState
from coire_core.settings import Settings

INSTANCE = uuid.uuid4()
NODE = uuid.uuid4()
ADMIN = Principal(kind=PrincipalKind.ADMIN, user_id=uuid.uuid4())


class Session:
    def __init__(self, *, active_job: bool = False, unreleased_lease: bool = False) -> None:
        now = datetime.now(UTC)
        self.instance = SimpleNamespace(
            id=INSTANCE,
            policy="image:coire-edge-b",
            state=InstanceState.READY,
            updated_at=now,
            transitioned_at=now,
        )
        self.active_job = active_job
        self.unreleased_lease = unreleased_lease
        self.scalar_calls = 0
        self.commits = 0
        self.reservation = SimpleNamespace(state=MemoryReservationState.HELD, released_at=None)

    async def execute(self, statement: object, parameters: object = None) -> None:
        pass

    async def get(self, model: type[object], identity: object, **kwargs: object) -> Any:
        if model is ModelInstanceRow:
            assert identity == INSTANCE
            return self.instance
        if model is NodeRow:
            assert identity == NODE
            return SimpleNamespace(id=NODE, name="coire-edge-b")
        raise AssertionError(model)

    async def scalar(self, statement: object) -> object | None:
        self.scalar_calls += 1
        if self.scalar_calls == 1:
            return "active" if self.active_job else None
        if self.scalar_calls == 2:
            return SimpleNamespace(instance_id=INSTANCE, node_id=NODE)
        if self.scalar_calls == 3:
            return "lease" if self.unreleased_lease else None
        assert self.scalar_calls == 4
        return self.reservation

    async def commit(self) -> None:
        self.commits += 1


def _request() -> Request:
    app = Starlette()
    app.state.settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    return Request(
        {
            "type": "http",
            "method": "DELETE",
            "path": f"/api/v1/admin/image-workers/{INSTANCE}",
            "headers": [],
            "app": app,
        }
    )


async def test_admin_unload_drains_then_confirms_exact_node_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    audits: list[str] = []
    stages: list[InstanceState] = []

    async def audit(_session: object, **kwargs: object) -> None:
        audits.append(str(kwargs["action"]))

    class Client:
        def __init__(self, settings: Settings) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def unload_image_worker(self, node: str, command: object) -> ImageWorkerLoadResult:
            assert node == "coire-edge-b"
            stages.append(session.instance.state)
            return ImageWorkerLoadResult(
                instance_id=INSTANCE,
                state="failed",
                reserved_bytes=0,
                safe_error="worker_stopped",
            )

    monkeypatch.setattr(admin_images, "write_audit", audit)
    monkeypatch.setattr(admin_images, "NodeClient", Client)
    response = Response()
    result = await admin_images.unload_admin_image_worker(
        INSTANCE, _request(), ADMIN, cast(AsyncSession, session), response
    )
    assert result.instance_id == INSTANCE
    assert stages == [InstanceState.DRAINING]
    assert session.instance.state is InstanceState.STOPPED
    assert session.reservation.state is MemoryReservationState.RELEASED
    assert session.reservation.released_at is not None
    assert session.commits == 2
    assert audits == ["image.worker.unload_requested", "image.worker.unloaded"]
    assert response.headers["cache-control"] == "private, no-store"


async def test_admin_unload_refuses_active_job_before_draining() -> None:
    session = Session(active_job=True)
    with pytest.raises(ImageConflict, match="active job"):
        await admin_images.unload_admin_image_worker(
            INSTANCE, _request(), ADMIN, cast(AsyncSession, session), Response()
        )
    assert session.instance.state is InstanceState.READY
    assert session.commits == 0


async def test_admin_unload_keeps_uncertain_execution_lease() -> None:
    session = Session(unreleased_lease=True)
    with pytest.raises(ImageConflict, match="unreleased execution lease"):
        await admin_images.unload_admin_image_worker(
            INSTANCE, _request(), ADMIN, cast(AsyncSession, session), Response()
        )
    assert session.instance.state is InstanceState.READY
    assert session.commits == 0


async def test_uncertain_node_stop_keeps_draining_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()

    async def audit(_session: object, **kwargs: object) -> None:
        pass

    class Client:
        def __init__(self, settings: Settings) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def unload_image_worker(self, node: str, command: object) -> ImageWorkerLoadResult:
            raise NodeError(NodeErrorKind.UNAVAILABLE, node)

    monkeypatch.setattr(admin_images, "write_audit", audit)
    monkeypatch.setattr(admin_images, "NodeClient", Client)
    with pytest.raises(ImageConflict, match="reconciliation"):
        await admin_images.unload_admin_image_worker(
            INSTANCE, _request(), ADMIN, cast(AsyncSession, session), Response()
        )
    assert session.instance.state is InstanceState.DRAINING
    assert session.reservation.state is MemoryReservationState.HELD
    assert session.commits == 1
