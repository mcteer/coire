"""Ops checks the durable Core fence before model work and exposes authenticated inventory."""

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from coire_ops import app as ops_app

from coire_core.settings import Settings


async def test_ops_inventory_is_private_and_fence_blocks_new_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ops_app, "get_settings", lambda: Settings(upgrades_enabled=True))
    app = ops_app.create_app(expected_token="ops-credential")
    service = AsyncMock()
    service.upgrade_admission.return_value = False
    app.state.ops_service = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ops"
    ) as client:
        assert (await client.get("/internal/upgrades/inventory")).status_code == 403
        result = await client.get(
            "/internal/upgrades/inventory", headers={"Authorization": "Bearer ops-credential"}
        )
        assert result.status_code == 200 and result.json()["owner"] == "ops"
        response = await client.post(
            "/turn",
            headers={"Authorization": "Bearer ops-credential"},
            json={"conversation_id": str(uuid4()), "question": "status"},
        )
        assert response.status_code == 503 and response.headers["retry-after"] == "5"
        service.turn.assert_not_called()
        assert app.state.active_turns == 0
