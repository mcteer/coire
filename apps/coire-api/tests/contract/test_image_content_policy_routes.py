"""Both submission surfaces refuse all roles without queuing or model work."""

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import get_session
from coire_api.images.authorization import require_image_principal
from coire_api.routes import images, v1_images
from coire_core.errors import CoireError
from coire_core.settings import Settings


@pytest.mark.parametrize("kind", [PrincipalKind.USER, PrincipalKind.ADMIN, PrincipalKind.API_KEY])
@pytest.mark.parametrize("compatible", [False, True])
async def test_both_routes_refuse_and_audit_without_job_or_inference(
    kind: PrincipalKind, compatible: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()
    app.state.settings = Settings(_secrets_dir="/nonexistent", image_enabled=True)  # type: ignore[call-arg]
    app.include_router(images.router)
    app.include_router(v1_images.router)
    principal = Principal(
        kind=kind,
        user_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        credential_version=1,
        scopes=frozenset({"images", "images:explicit"}),
    )
    app.dependency_overrides[require_image_principal] = lambda: principal
    session = AsyncMock(spec=AsyncSession)

    async def dependency() -> Any:
        yield session

    app.dependency_overrides[get_session] = dependency
    audit = AsyncMock()
    monkeypatch.setattr(images, "audit_image_refusal", audit)
    monkeypatch.setattr(v1_images, "audit_image_refusal", audit)

    @app.exception_handler(CoireError)
    async def problem(_: Request, exc: CoireError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content=exc.to_problem().model_dump(mode="json")
        )

    body = {
        "prompt": "sexualized depiction of a minor",
        "model" if compatible else "model_id": str(uuid.uuid4()),
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(
            "/v1/images/generations" if compatible else "/api/v1/images",
            json=body,
            headers={"Idempotency-Key": "safety-route-test"},
        )
    assert result.status_code == 403
    assert result.json()["type"].endswith("image_content_blocked")
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()
    audit.assert_awaited_once()
    assert "prompt" not in str(audit.call_args)
