"""Compatible image generation uses native admission and does not invent a second path."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import get_session
from coire_api.images import compatible, jobs
from coire_api.images.authorization import require_image_principal
from coire_api.routes import images, v1_images
from coire_core.errors import CoireError, ImageTimeout, ImageValidationError
from coire_core.models.images import (
    ImageContentMode,
    ImageJob,
    ImageJobReceipt,
    ImageJobState,
    ImageSpec,
    OpenAIImageGenerationRequest,
)
from coire_core.settings import Settings

JOB = "01J00000000000000000000000"
MODEL = uuid.uuid4()
OWNER = uuid.uuid4()


def test_native_submit_maps_supported_fields_and_rejects_quality() -> None:
    body = OpenAIImageGenerationRequest(
        model=MODEL, prompt="a studio", size="512x768", n=2, coire_seed=9
    )
    native = compatible.native_submit(body)
    assert native.model_id == MODEL
    assert native.width == 512
    assert native.height == 768
    assert native.n == 2
    assert native.seed == 9
    with pytest.raises(ImageValidationError):
        compatible.native_submit(
            OpenAIImageGenerationRequest(model=MODEL, prompt="a studio", quality="hd")
        )


def _job(state: ImageJobState) -> ImageJob:
    spec = ImageSpec(
        model_id=MODEL,
        prompt="a studio",
        width=64,
        height=64,
        steps=4,
        guidance=Decimal(0),
        seed=9,
        content_mode=ImageContentMode.STANDARD,
    )
    now = datetime.now(UTC)
    return ImageJob(
        id=JOB,
        state=state,
        effective_spec=spec,
        resolved=None,
        failure_code=None if state is not ImageJobState.FAILED else "worker_unavailable",
        created_at=now,
        updated_at=now,
    )


def _app() -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(
        _secrets_dir="/nonexistent", image_enabled=True, image_compatible_wait_s=1
    )  # type: ignore[call-arg]
    app.include_router(v1_images.router)
    app.include_router(images.router)
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    app.dependency_overrides[require_image_principal] = lambda: principal

    async def fake_session():  # type: ignore[no-untyped-def]
        yield object()

    app.dependency_overrides[get_session] = fake_session

    @app.exception_handler(CoireError)
    async def problem(_: Request, exc: CoireError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content=exc.to_problem().model_dump(mode="json")
        )

    return app


async def test_timeout_returns_job_id_without_a_success_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def admit(*_args: object, **_kwargs: object) -> str:
        return JOB

    async def wait(*_args: object, **_kwargs: object) -> ImageJob:
        raise ImageTimeout(JOB)

    monkeypatch.setattr(v1_images, "admit_compatible_job", admit)
    monkeypatch.setattr(v1_images, "await_image_generation", wait)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        result = await client.post(
            "/v1/images/generations",
            json={"model": str(MODEL), "prompt": "a studio"},
        )
    assert result.status_code == 504
    body = result.json()
    assert body["coire_job_id"] == JOB
    assert "data" not in body


async def test_compatible_timeout_job_is_recoverable_from_native_owner_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def admit(*_args: object, **_kwargs: object) -> str:
        return JOB

    async def wait(*_args: object, **_kwargs: object) -> ImageJob:
        raise ImageTimeout(JOB)

    async def read(*_args: object, **_kwargs: object) -> ImageJob:
        return _job(ImageJobState.QUEUED)

    monkeypatch.setattr(v1_images, "admit_compatible_job", admit)
    monkeypatch.setattr(v1_images, "await_image_generation", wait)
    monkeypatch.setattr(jobs, "get_owned_image_job", read)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        timed_out = await client.post(
            "/v1/images/generations", json={"model": str(MODEL), "prompt": "a studio"}
        )
        recovered = await client.get(f"/api/v1/images/{JOB}")
    assert timed_out.status_code == 504
    assert timed_out.json()["coire_job_id"] == JOB
    assert recovered.status_code == 200
    assert recovered.json()["id"] == JOB
    assert recovered.json()["state"] == "queued"


async def test_failed_job_is_an_error_with_the_same_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async def admit(*_args: object, **_kwargs: object) -> str:
        return JOB

    async def wait(*_args: object, **_kwargs: object) -> ImageJob:
        return _job(ImageJobState.FAILED)

    monkeypatch.setattr(v1_images, "admit_compatible_job", admit)
    monkeypatch.setattr(v1_images, "await_image_generation", wait)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        result = await client.post(
            "/v1/images/generations", json={"model": str(MODEL), "prompt": "a studio"}
        )
    assert result.status_code == 409
    assert result.json()["coire_job_id"] == JOB
    assert result.json()["detail"] == "worker_unavailable"


def test_receipt_shape_is_not_the_compatible_success_type() -> None:
    receipt = ImageJobReceipt(job_id=JOB, state=ImageJobState.QUEUED)
    assert "data" not in receipt.model_dump()
