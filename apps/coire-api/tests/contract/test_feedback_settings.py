"""Feedback settings retain the native Chat credential and origin boundary."""

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.routes.chat_feedback import router
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "principal",
    [
        Principal(),
        Principal(kind=PrincipalKind.SERVICE),
        Principal(kind=PrincipalKind.RUN, user_id=uuid.uuid4()),
        Principal(kind=PrincipalKind.API_KEY, user_id=uuid.uuid4(), scopes=frozenset({"mcp"})),
    ],
)
async def test_settings_refuse_non_chat_credentials(principal: Principal) -> None:
    app = FastAPI()
    app.state.settings = Settings(
        _secrets_dir="/nonexistent", chat_enabled=True, chat_browser_origin="https://chat.test"
    )  # type: ignore[call-arg]
    app.dependency_overrides[require_principal] = lambda: principal
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://chat.test"
    ) as client:
        assert (await client.get("/api/v1/chat/feedback-preference")).status_code in {401, 403}


async def test_settings_mutation_requires_same_origin() -> None:
    app = FastAPI()
    app.state.settings = Settings(
        _secrets_dir="/nonexistent", chat_enabled=True, chat_browser_origin="https://chat.test"
    )  # type: ignore[call-arg]
    app.dependency_overrides[require_principal] = lambda: Principal(
        kind=PrincipalKind.USER, user_id=uuid.uuid4()
    )
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://chat.test"
    ) as client:
        assert (
            await client.patch(
                "/api/v1/chat/feedback-preference",
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "expected_version": 1,
                    "enabled": False,
                    "disclosure_version": "feedback-v1",
                },
            )
        ).status_code == 403
