"""Public training schemas/recipe boundaries; transaction cases use real Postgres separately."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.routes.admin_adapters import router as adapters_router
from coire_api.routes.admin_training import router as training_router
from coire_api.training.authorization import require_training_principal
from coire_api.training.service import decode_page_cursor, encode_page_cursor
from coire_api.training.specs import parse_spec
from coire_core.errors import CoireError, TrainingNotFound, TrainingValidationError
from coire_core.models.adapters import AdapterReceipt, AdapterState
from coire_core.models.auth import UserRole
from coire_core.settings import Settings


def application() -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(training_enabled=True)
    app.include_router(training_router)
    app.include_router(adapters_router)
    app.dependency_overrides[require_training_principal] = lambda: Principal(
        kind=PrincipalKind.USER, user_id=uuid.uuid4(), role=UserRole.ADMIN
    )
    return app


@pytest.mark.parametrize("enabled", [True, False])
async def test_promotion_uses_installed_extraction_without_unset_app_flag(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    app = application()
    app.state.settings.training_enabled = enabled

    @app.exception_handler(CoireError)
    async def problem(request: Request, exc: CoireError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @asynccontextmanager
    async def session() -> AsyncIterator[AsyncSession]:
        yield AsyncMock(spec=AsyncSession)

    adapter_id = uuid.uuid4()
    enqueue = AsyncMock(
        return_value=AdapterReceipt(adapter_id=adapter_id, state=AdapterState.VALIDATING, version=1)
    )
    monkeypatch.setattr("coire_api.routes.admin_training.session_scope", session)
    monkeypatch.setattr("coire_api.training.adapters.enqueue_checkpoint_promotion", enqueue)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/admin/training/checkpoints/{uuid.uuid4()}/promote",
            json={"expected_version": 1, "adapter_slug": "checkpoint-acceptance"},
            headers={"Idempotency-Key": "promotion-wiring"},
        )
    if enabled:
        assert response.status_code == 202
        assert response.json()["adapter_id"] == str(adapter_id)
        assert enqueue.await_args is not None
        assert enqueue.await_args.kwargs["extraction_available"] is True
    else:
        assert response.status_code == 503
        enqueue.assert_not_awaited()


async def test_versioned_recipes_are_unbound_and_all_public_contracts_are_registered() -> None:
    app = application()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/admin/training/recipes")
    assert response.status_code == 200
    recipes = response.json()["items"]
    assert {recipe["parameterization"] for recipe in recipes} == {"lora", "qlora", "dora"}
    assert {recipe["id"] for recipe in recipes} == {
        "sft-lora",
        "sft-qlora",
        "sft-dora",
        "sft-evaluated",
    }
    for recipe in recipes:
        assert recipe["version"] == 1
        assert len(recipe["required_bindings"]) == (6 if recipe["id"] == "sft-evaluated" else 4)
        with pytest.raises(TrainingValidationError):
            parse_spec(recipe["template_yaml"])
    paths = app.openapi()["paths"]
    assert paths["/api/v1/admin/training/jobs"]["post"]["responses"]["202"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/TrainingJobReceipt")
    assert "/api/v1/admin/training/checkpoints/{checkpoint_id}/promote" in paths
    assert "/api/v1/admin/training/measurements/{measurement_id}" in paths
    assert "/api/v1/admin/training/profiles" in paths
    assert paths["/api/v1/admin/adapters/{adapter_id}"]["patch"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/AdapterDetail")
    assert paths["/api/v1/admin/adapters/{adapter_id}/retire"]["post"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]["$ref"].endswith("/AdapterDetail")
    assert paths["/api/v1/admin/adapters/{adapter_id}"]["delete"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/AdapterReceipt")


def test_page_cursors_are_versioned_scoped_expiring_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    monkeypatch.setattr("coire_api.training.service.time.time", lambda: 1000.0)
    cursor = encode_page_cursor("profiles", now, str(uuid.uuid4()))
    assert decode_page_cursor(cursor, "profiles")[0] == now
    for token, scope in ((cursor, "adapters"), ("a" * 513, "profiles"), ("not-base64", "profiles")):
        with pytest.raises(TrainingValidationError):
            decode_page_cursor(token, scope)
    monkeypatch.setattr("coire_api.training.service.time.time", lambda: 1900.0)
    with pytest.raises(TrainingValidationError, match="expired"):
        decode_page_cursor(cursor, "profiles")


@pytest.mark.parametrize("operation", ["pause", "cancel", "resume"])
async def test_disabled_training_preserves_stop_lane_but_blocks_resume(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    app = application()
    app.state.settings.training_enabled = False

    @app.exception_handler(CoireError)
    async def problem(_request: Request, error: CoireError) -> JSONResponse:
        return JSONResponse(
            {"detail": str(error)}, status_code=404 if isinstance(error, TrainingNotFound) else 503
        )

    @asynccontextmanager
    async def session() -> AsyncIterator[AsyncSession]:
        yield AsyncMock(spec=AsyncSession)

    control = AsyncMock(side_effect=TrainingNotFound())
    monkeypatch.setattr("coire_api.routes.admin_training.session_scope", session)
    monkeypatch.setattr("coire_api.routes.admin_training.control_training", control)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/admin/training/jobs/01ARZ3NDEKTSV4RRFFQ69G5FAV/{operation}",
            json={"expected_version": 1},
            headers={"Idempotency-Key": "disable-control"},
        )
    if operation == "resume":
        assert response.status_code == 503
        control.assert_not_awaited()
    else:
        assert response.status_code == 404
        assert control.await_args is not None and control.await_args.args[3] == operation
