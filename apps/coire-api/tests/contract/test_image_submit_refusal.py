"""A late explicit admission refusal releases locks and commits a private audit."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import get_session
from coire_api.images import admission
from coire_api.images.authorization import require_image_principal
from coire_api.routes import images
from coire_core.errors import CoireError, ImageForbidden


async def test_explicit_admission_refusal_rolls_back_before_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    app = FastAPI()
    app.include_router(images.router)
    app.dependency_overrides[require_image_principal] = lambda: principal
    order: list[str] = []
    session = AsyncMock()

    async def rollback() -> None:
        order.append("rollback")

    session.rollback.side_effect = rollback

    async def get_db():  # type: ignore[no-untyped-def]
        yield session

    app.dependency_overrides[get_session] = get_db

    @app.exception_handler(CoireError)
    async def problem(request: Request, error: CoireError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content=error.to_problem().model_dump())

    admit = AsyncMock(side_effect=ImageForbidden())
    monkeypatch.setattr(admission, "admit_image_job", admit)

    async def audit(actor: Principal, **kwargs: str) -> None:
        assert actor == principal
        assert kwargs == {
            "action": "image.refused",
            "target_type": "route",
            "target_id": "POST /api/v1/images",
        }
        assert "private anatomy prompt" not in str(kwargs)
        order.append("audit")

    monkeypatch.setattr(images, "audit_image_refusal", audit)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/images",
            json={
                "schema_version": 1,
                "model_id": str(uuid.uuid4()),
                "prompt": "private anatomy prompt",
                "content_mode": "explicit",
            },
            headers={"Idempotency-Key": "explicit-refusal-test"},
        )
    assert response.status_code == 403
    assert response.json()["coire_code"] == "image_forbidden"
    assert order == ["rollback", "audit"]
    session.commit.assert_not_awaited()
    admit.assert_awaited_once()
