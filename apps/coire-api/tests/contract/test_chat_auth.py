"""Native chat credential, same-origin and owner boundaries."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from coire_api.auth import (
    CurrentChatUser,
    Principal,
    PrincipalKind,
    require_owned_chat,
    require_principal,
)
from coire_api.db import ChatConversationRow
from coire_core.errors import ChatNotFound
from coire_core.settings import Settings


def _app(principal: Principal) -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(
        _secrets_dir="/nonexistent", chat_browser_origin="https://chat.example.test"
    )  # type: ignore[call-arg]
    app.dependency_overrides[require_principal] = lambda: principal

    @app.get("/chat")
    async def read(user: CurrentChatUser) -> dict[str, str]:
        return {"user_id": str(user.user_id)}

    @app.post("/chat")
    async def mutate(user: CurrentChatUser) -> dict[str, str]:
        return {"user_id": str(user.user_id)}

    return app


@pytest.mark.parametrize(
    "principal",
    [
        Principal(),
        Principal(kind=PrincipalKind.RUN, user_id=uuid.uuid4()),
        Principal(kind=PrincipalKind.OPS_SERVICE),
        Principal(kind=PrincipalKind.SERVICE),
        Principal(kind=PrincipalKind.ADMIN),
        Principal(kind=PrincipalKind.API_KEY, user_id=uuid.uuid4(), scopes=frozenset({"mcp"})),
    ],
)
async def test_chat_rejects_non_user_or_wrong_scope(principal: Principal) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://chat.example.test"
    ) as client:
        response = await client.get("/chat")
    assert response.status_code in {401, 403}


async def test_browser_mutation_requires_exact_configured_origin() -> None:
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://chat.example.test"
    ) as client:
        assert (await client.get("/chat")).status_code == 200
        assert (await client.post("/chat")).status_code == 403
        assert (
            await client.post("/chat", headers={"Origin": "https://evil.example"})
        ).status_code == 403
        assert (
            await client.post("/chat", headers={"Origin": "https://chat.example.test"})
        ).status_code == 200


async def test_scoped_user_api_key_is_not_browser_origin_bound() -> None:
    principal = Principal(
        kind=PrincipalKind.API_KEY, user_id=uuid.uuid4(), scopes=frozenset({"chat"})
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://chat.example.test"
    ) as client:
        assert (await client.post("/chat")).status_code == 200


async def test_foreign_missing_and_deleted_conversations_share_404() -> None:
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    conversation_id = uuid.uuid4()
    row = ChatConversationRow(
        id=conversation_id,
        owner_user_id=owner_id,
        title="Private",
        mode="chat",
        revision=1,
        deleted_at=None,
    )

    class Session:
        def __init__(self, result: ChatConversationRow | None) -> None:
            self.result = result

        async def get(self, model: object, identifier: uuid.UUID) -> ChatConversationRow | None:
            return self.result

    for result, user_id in ((None, owner_id), (row, other_id)):
        with pytest.raises(ChatNotFound) as error:
            await require_owned_chat(
                Session(result),
                conversation_id,
                Principal(kind=PrincipalKind.USER, user_id=user_id),
            )  # type: ignore[arg-type]
        assert error.value.to_problem().status == 404
    row.deleted_at = datetime.now(UTC)
    with pytest.raises(ChatNotFound):
        await require_owned_chat(
            Session(row), conversation_id, Principal(kind=PrincipalKind.ADMIN, user_id=owner_id)
        )  # type: ignore[arg-type]
