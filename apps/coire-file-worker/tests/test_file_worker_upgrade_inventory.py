"""Strict CPU ownership is observable only with the existing worker credential."""

import httpx
from pydantic import SecretStr

from coire_core.settings import Settings
from coire_file_worker.app import create_app


async def test_worker_inventory_preserves_native_parser_ownership() -> None:
    app = create_app(Settings(file_worker_service_token=SecretStr("worker-secret")))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        assert (await client.get("/internal/upgrades/inventory")).status_code == 401
        headers = {"Authorization": "Bearer worker-secret"}
        result = await client.get("/internal/upgrades/inventory", headers=headers)
        assert result.status_code == 200 and result.json()["entries"] == []
        app.state.worker.recipe_active = True
        result = await client.get("/internal/upgrades/inventory", headers=headers)
        assert result.json()["owner"] == "files"
        assert result.json()["entries"][0]["state"] == "active"
        app.state.worker.recipe_active = False
