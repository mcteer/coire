"""Native cancellation returns a durable intent or an already terminal job."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import get_session
from coire_api.images import cancellation
from coire_api.images.authorization import require_image_principal
from coire_api.routes import images
from coire_core.errors import CoireError, ImageNotFound
from coire_core.models.images import ImageJob, ImageJobState, ImageSpec

OWNER = uuid.uuid4()
JOB = "01J00000000000000000000000"


def _job(state: ImageJobState) -> ImageJob:
    now = datetime.now(UTC)
    return ImageJob(
        id=JOB,
        state=state,
        effective_spec=ImageSpec(
            model_id=uuid.uuid4(),
            prompt="private subject",
            width=512,
            height=512,
            steps=9,
            guidance=Decimal(0),
            seed=7,
        ),
        resolved=None,
        latest_event_sequence=2,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize(
    "terminal, expected_status",
    [(False, 202), (True, 200)],
)
async def test_cancel_route_status_and_private_cache(
    monkeypatch: pytest.MonkeyPatch, terminal: bool, expected_status: int
) -> None:
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    app = FastAPI()
    app.include_router(images.router)
    app.dependency_overrides[require_image_principal] = lambda: principal

    async def session():  # type: ignore[no-untyped-def]
        yield object()

    app.dependency_overrides[get_session] = session

    async def cancel(db: object, actor: Principal, job_id: str) -> tuple[ImageJob, bool]:
        assert job_id == JOB and actor == principal
        return _job(ImageJobState.CANCELLED if terminal else ImageJobState.CANCELLING), terminal

    monkeypatch.setattr(cancellation, "request_image_job_cancel", cancel)
    assert "delete" in app.openapi()["paths"]["/api/v1/images/{job_id}"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(f"/api/v1/images/{JOB}")
    assert response.status_code == expected_status
    assert response.json()["state"] == ("cancelled" if terminal else "cancelling")
    assert response.headers["cache-control"] == "private, no-store"


async def test_cancel_route_hides_unowned_job(monkeypatch: pytest.MonkeyPatch) -> None:
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    app = FastAPI()

    @app.exception_handler(CoireError)
    async def problem(request: object, error: CoireError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content=error.to_problem().model_dump())

    app.include_router(images.router)
    app.dependency_overrides[require_image_principal] = lambda: principal

    async def session():  # type: ignore[no-untyped-def]
        yield object()

    app.dependency_overrides[get_session] = session

    async def missing(db: object, actor: Principal, job_id: str) -> tuple[ImageJob, bool]:
        raise ImageNotFound()

    monkeypatch.setattr(cancellation, "request_image_job_cancel", missing)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(f"/api/v1/images/{JOB}")
    assert response.status_code == 404
