"""Private image job reads keep owner and live explicit authority."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ImageJobRow, ImageOutputRow, UserRow, get_session
from coire_api.images import events, jobs
from coire_api.images.authorization import require_image_principal
from coire_api.routes import images
from coire_core.errors import CoireError, ImageNotFound
from coire_core.models.images import (
    ImageContentMode,
    ImageJobEvent,
    ImageJobSettingsSnapshot,
    ImageJobState,
    ImageRecipe,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_core.settings import Settings

JOB = "01J00000000000000000000000"
OWNER = uuid.uuid4()


class FakeScalars:
    def __init__(self, items: list[Any] | None = None) -> None:
        self.items = items or []

    def all(self) -> list[Any]:
        return self.items


class FakeSession:
    def __init__(self, *, explicit: bool = False) -> None:
        spec = ImageSpec(
            model_id=uuid.uuid4(),
            prompt="owner private prompt",
            width=64,
            height=64,
            steps=4,
            guidance=Decimal(0),
            seed=7,
            content_mode=ImageContentMode.EXPLICIT if explicit else ImageContentMode.STANDARD,
        )
        now = datetime.now(UTC)
        self.row = ImageJobRow(
            id=JOB,
            owner_user_id=OWNER,
            state="queued",
            resolved_spec=ImageJobSettingsSnapshot(effective_spec=spec).model_dump(mode="json"),
            authorization_snapshot={
                "required_entitlements": ["explicit"] if explicit else [],
                "explicit": explicit,
            },
            safe_failure_code=None,
            created_at=now,
            updated_at=now,
        )
        self.outputs: list[ImageOutputRow] = []

    async def get(self, model: type[object], identity: object, **_: object) -> Any:
        if model is ImageJobRow:
            return self.row if identity == JOB else None
        if model is UserRow:
            return SimpleNamespace(active=True)
        raise AssertionError(model)

    async def scalar(self, _: object) -> int:
        return 1

    async def scalars(self, query: object) -> FakeScalars:
        if "FROM image_outputs" in str(query):
            return FakeScalars(self.outputs)
        if "FROM image_jobs" in str(query):
            return FakeScalars([self.row])
        return FakeScalars()


def _app(session: FakeSession, principal: Principal) -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(_secrets_dir="/nonexistent", image_enabled=False)  # type: ignore[call-arg]
    app.include_router(images.router)
    app.dependency_overrides[require_image_principal] = lambda: principal

    async def fake_session() -> Any:
        yield session

    app.dependency_overrides[get_session] = fake_session

    @app.exception_handler(CoireError)
    async def problem(_: Request, exc: CoireError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content=exc.to_problem().model_dump(mode="json")
        )

    return app


async def test_job_snapshot_is_owner_scoped_and_presets_route_keeps_precedence() -> None:
    session = FakeSession()
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    async with AsyncClient(
        transport=ASGITransport(app=_app(session, principal)), base_url="http://test"
    ) as client:
        result = await client.get(f"/api/v1/images/{JOB}")
        presets = await client.get("/api/v1/images/presets")
    assert result.status_code == 200
    assert result.json()["effective_spec"]["prompt"] == "owner private prompt"
    assert result.json()["latest_event_sequence"] == 1
    assert result.headers["cache-control"] == "private, no-store"
    assert presets.status_code == 200

    other = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    async with AsyncClient(
        transport=ASGITransport(app=_app(session, other)), base_url="http://test"
    ) as client:
        hidden = await client.get(f"/api/v1/images/{JOB}")
    assert hidden.status_code == 404


async def test_explicit_job_read_rechecks_live_entitlement() -> None:
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    async with AsyncClient(
        transport=ASGITransport(app=_app(FakeSession(explicit=True), principal)),
        base_url="http://test",
    ) as client:
        revoked = await client.get(f"/api/v1/images/{JOB}")
    assert revoked.status_code == 403


async def test_job_list_is_owner_bound_and_omits_revoked_explicit_jobs() -> None:
    session = FakeSession()
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    async with AsyncClient(
        transport=ASGITransport(app=_app(session, principal)), base_url="http://test"
    ) as client:
        listed = await client.get("/api/v1/images?limit=1")
        malformed = await client.get("/api/v1/images?cursor=bad")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [JOB]
    assert listed.headers["cache-control"] == "private, no-store"
    assert malformed.status_code == 404

    explicit_session = FakeSession(explicit=True)
    async with AsyncClient(
        transport=ASGITransport(app=_app(explicit_session, principal)), base_url="http://test"
    ) as client:
        hidden = await client.get("/api/v1/images")
    assert hidden.status_code == 200
    assert hidden.json()["items"] == []

    cursor = jobs._encode_cursor(session.row)
    assert jobs._decode_cursor(cursor, OWNER)[1] == JOB
    with pytest.raises(ImageNotFound):
        jobs._decode_cursor(cursor, uuid.uuid4())


async def test_completed_job_includes_only_safe_published_output_projection() -> None:
    session = FakeSession()
    effective = ImageJobSettingsSnapshot.model_validate(session.row.resolved_spec).effective_spec
    resolved = ResolvedImageSpec(
        spec=effective,
        seeds=(7,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(effective),
    )
    recipe = ImageRecipe(
        resolved=resolved,
        output_index=0,
        seed=7,
        pixel_sha256="c" * 64,
        width=64,
        height=64,
    )
    session.row.state = "succeeded"
    session.row.resolved_spec = ImageJobSettingsSnapshot(
        effective_spec=effective, resolved=resolved
    ).model_dump(mode="json")
    session.outputs.append(
        ImageOutputRow(
            id=uuid.uuid4(),
            owner_user_id=OWNER,
            job_id=JOB,
            output_index=0,
            blob_key="private/never-return.png",
            size_bytes=12,
            file_sha256="d" * 64,
            pixel_sha256="c" * 64,
            recipe=recipe.model_dump(mode="json"),
            content_tag="normal",
            classifier_provenance={
                "tag": "normal",
                "score": "0.1",
                "classifier_revision": "a" * 40,
                "processor_sha256": "b" * 64,
                "tagged_at": datetime.now(UTC).isoformat(),
            },
            entitlement_snapshot={},
            state="published",
            created_at=datetime.now(UTC),
        )
    )
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    async with AsyncClient(
        transport=ASGITransport(app=_app(session, principal)), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/v1/images/{JOB}")
    assert response.status_code == 200
    assert response.json()["outputs"][0]["index"] == 0
    assert "blob_key" not in response.text
    session.outputs[0].content_tag = "explicit"
    async with AsyncClient(
        transport=ASGITransport(app=_app(session, principal)), base_url="http://test"
    ) as client:
        filtered = await client.get(f"/api/v1/images/{JOB}")
    assert filtered.status_code == 200
    assert filtered.json()["outputs"] == []


async def test_event_route_refuses_foreign_or_malformed_cursor_before_stream() -> None:
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    async with AsyncClient(
        transport=ASGITransport(app=_app(FakeSession(), principal)), base_url="http://test"
    ) as client:
        wrong_job = await client.get(
            f"/api/v1/images/{JOB}/events",
            headers={"Last-Event-ID": "01J00000000000000000000001:1"},
        )
        malformed = await client.get(f"/api/v1/images/{JOB}/events", headers={"Last-Event-ID": "1"})
        too_long = await client.get(
            f"/api/v1/images/{JOB}/events", headers={"Last-Event-ID": "x" * 51}
        )
    assert wrong_job.status_code == 400
    assert malformed.status_code == 400
    assert too_long.status_code == 400


async def test_event_route_replays_terminal_event_with_private_sse_headers(
    monkeypatch: Any,
) -> None:
    session = FakeSession()
    session.row.state = "failed"
    session.row.safe_failure_code = "worker_unavailable"
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    snapshot = await jobs.get_owned_image_job(cast(AsyncSession, session), principal, JOB)
    event = ImageJobEvent(
        job_id=JOB,
        sequence=1,
        at=datetime.now(UTC),
        type="error",
        state=ImageJobState.FAILED,
        safe_code="worker_unavailable",
    )

    @asynccontextmanager
    async def fake_scope() -> Any:
        yield session

    async def fake_read(*args: object) -> tuple[Any, list[ImageJobEvent]]:
        return snapshot, [event]

    monkeypatch.setattr(images, "session_scope", fake_scope)
    monkeypatch.setattr(events, "read_owned_image_events", fake_read)
    async with AsyncClient(
        transport=ASGITransport(app=_app(session, principal)), base_url="http://test"
    ) as client:
        result = await client.get(f"/api/v1/images/{JOB}/events")
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("text/event-stream")
    assert result.headers["cache-control"] == "private, no-store"
    assert result.headers["x-accel-buffering"] == "no"
    assert f"id: {JOB}:1\nevent: error\n" in result.text


def test_event_openapi_describes_stream_and_typed_frame_data() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    response = document["paths"]["/api/v1/images/{job_id}/events"]["get"]["responses"]["200"]
    assert set(response["content"]) == {"text/event-stream"}
    assert response["content"]["text/event-stream"]["schema"] == {"type": "string"}
    assert response["content"]["text/event-stream"]["x-coire-event-schema"] == {
        "$ref": "#/components/schemas/ImageJobEvent"
    }
    assert "ImageJobEvent" in document["components"]["schemas"]
