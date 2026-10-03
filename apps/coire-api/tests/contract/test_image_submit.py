"""Native image submission returns the admitted job and does not start a second one."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import get_session
from coire_api.images import admission
from coire_api.images.authorization import require_image_principal
from coire_api.routes import images
from coire_core.errors import CoireError
from coire_core.models.images import ImageJobReceipt, ImageJobState, ImageSubmitRequest
from coire_core.settings import Settings

MODEL = uuid.uuid4()
JOB = "01J00000000000000000000000"


async def test_submit_returns_the_admitted_receipt(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: list[tuple[ImageSubmitRequest, str]] = []

    async def admit(
        session: object,
        principal: Principal,
        request: ImageSubmitRequest,
        idempotency_key: str,
        settings: Settings,
    ) -> ImageJobReceipt:
        del session, principal, settings
        seen.append((request, idempotency_key))
        return ImageJobReceipt(job_id=JOB, state=ImageJobState.QUEUED, event_cursor="1")

    monkeypatch.setattr(admission, "admit_image_job", admit)
    app = FastAPI()
    app.state.settings = Settings(_secrets_dir="/nonexistent", image_enabled=True)  # type: ignore[call-arg]
    app.include_router(images.router)
    app.dependency_overrides[require_image_principal] = lambda: Principal(
        kind=PrincipalKind.USER, user_id=uuid.uuid4()
    )

    async def fake_session():  # type: ignore[no-untyped-def]
        yield object()

    app.dependency_overrides[get_session] = fake_session

    @app.exception_handler(CoireError)
    async def problem(_: Request, exc: CoireError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content=exc.to_problem().model_dump(mode="json")
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(
            "/api/v1/images",
            headers={"Idempotency-Key": "submit-key-1"},
            json={"schema_version": 1, "model_id": str(MODEL), "prompt": "a quiet portrait"},
        )
    assert result.status_code == 202
    assert result.json()["job_id"] == JOB
    assert result.json()["state"] == "queued"
    assert result.headers["cache-control"] == "private, no-store"
    assert seen[0][1] == "submit-key-1"
    assert seen[0][0].prompt == "a quiet portrait"
