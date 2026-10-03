"""Cross-owner image inspection exposes status only and kill uses the fenced path."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import get_session
from coire_api.images import cancellation
from coire_api.routes import admin_console, admin_images
from coire_core.models.console import ActivityKind
from coire_core.models.images import ImageJobSettingsSnapshot, ImageSpec
from coire_core.models.instance import InstanceState

JOB = "01J00000000000000000000000"
OWNER = uuid.uuid4()
ADMIN = Principal(kind=PrincipalKind.ADMIN, user_id=uuid.uuid4())


class Rows:
    def __init__(self, items: list[object]) -> None:
        self.items = items

    def all(self) -> list[object]:
        return self.items


class Session:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        spec = ImageSpec(
            model_id=uuid.uuid4(),
            prompt="private owner prompt",
            width=512,
            height=512,
            steps=4,
            guidance=Decimal(0),
            seed=3,
        )
        self.row = SimpleNamespace(
            id=JOB,
            owner_user_id=OWNER,
            state="running",
            queued_at=now,
            updated_at=now,
            safe_failure_code=None,
            resolved_spec=ImageJobSettingsSnapshot(effective_spec=spec).model_dump(mode="json"),
            authorization_snapshot={"required_entitlements": [], "explicit": False},
        )
        self.items: list[object] = [self.row]
        self.commits = 0

    async def scalars(self, statement: object) -> Rows:
        return Rows(self.items)

    async def get(self, model: type[object], identity: object, **kwargs: object) -> object | None:
        return self.row if identity == JOB else None

    async def commit(self) -> None:
        self.commits += 1


async def test_admin_image_inspect_and_kill_routes_are_private_and_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    app = FastAPI()
    app.include_router(admin_images.jobs_router)
    app.dependency_overrides[admin_images.require_human_image_admin] = lambda: ADMIN

    async def fake_session() -> Any:
        yield session

    app.dependency_overrides[get_session] = fake_session
    audits: list[str] = []

    async def audit(_session: object, **kwargs: object) -> None:
        audits.append(str(kwargs["action"]))

    async def cancel(*args: object) -> tuple[object, bool]:
        session.row.state = "cancelling"
        return object(), False

    monkeypatch.setattr(admin_images, "write_audit", audit)
    monkeypatch.setattr(cancellation, "request_admin_image_job_cancel", cancel)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://coire.test"
    ) as client:
        listed = await client.get("/api/v1/admin/image-jobs")
        detail = await client.get(f"/api/v1/admin/image-jobs/{JOB}")
        killed = await client.delete(f"/api/v1/admin/image-jobs/{JOB}")
    assert listed.status_code == detail.status_code == 200
    assert killed.status_code == 202
    assert all(
        response.headers["cache-control"] == "private, no-store"
        for response in (listed, detail, killed)
    )
    assert listed.json()["items"][0]["owner_id"] == str(OWNER)
    assert detail.json()["job_id"] == JOB
    assert killed.json()["state"] == "cancelling"
    assert "private owner prompt" not in str((listed.json(), detail.json(), killed.json()))
    assert audits == ["image.admin.list", "image.admin.inspect"]
    assert session.commits == 2


async def test_admin_image_activity_has_stable_bounded_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    older = SimpleNamespace(**vars(session.row))
    older.id = "01J00000000000000000000001"
    session.items.append(older)
    app = FastAPI()
    app.include_router(admin_images.jobs_router)
    app.dependency_overrides[admin_images.require_human_image_admin] = lambda: ADMIN

    async def fake_session() -> Any:
        yield session

    async def audit(_session: object, **kwargs: object) -> None:
        pass

    app.dependency_overrides[get_session] = fake_session
    monkeypatch.setattr(admin_images, "write_audit", audit)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://coire.test"
    ) as client:
        result = await client.get("/api/v1/admin/image-jobs?limit=1")
    assert result.status_code == 200
    assert len(result.json()["items"]) == 1
    assert result.json()["next_cursor"].endswith(f"|{JOB}")


async def test_console_activity_identifies_image_workers_for_the_admin_unload_action() -> None:
    model_id = uuid.uuid4()
    instance_id = uuid.uuid4()
    instance = SimpleNamespace(
        id=instance_id,
        model_id=model_id,
        policy="image:coire-edge-b",
        state=InstanceState.READY,
        created_at=datetime.now(UTC),
        failure_detail=None,
    )

    class Result:
        def __init__(self, rows: list[object]) -> None:
            self.rows = rows

        def scalars(self) -> Rows:
            return Rows(self.rows)

    class ActivitySession:
        async def execute(self, statement: object) -> Result:
            sql = str(statement)
            if "FROM model_instances" in sql:
                return Result([instance])
            if "FROM models" in sql:
                return Result([SimpleNamespace(id=model_id, display_name="Image base")])
            return Result([])

    page = await admin_console.console_activity(
        ADMIN, cast(AsyncSession, ActivitySession()), limit=50
    )
    assert page.items[0].kind is ActivityKind.IMAGE_WORKER
    assert page.items[0].id == instance_id
    assert page.items[0].target == "Image base"
