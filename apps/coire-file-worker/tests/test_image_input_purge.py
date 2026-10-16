"""Normalized input deletion stays within the authenticated worker's writable root."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from coire_core.settings import Settings
from coire_file_worker.app import create_app


@pytest.mark.asyncio
async def test_image_input_purge_auth_binding_idempotence_and_no_follow(tmp_path: Path) -> None:
    settings = Settings(
        _secrets_dir="/nonexistent",
        file_worker_service_token=SecretStr("private-test"),
        file_worker_image_output_root=str(tmp_path),
    )  # type: ignore[call-arg]
    app = create_app(settings)
    input_id = uuid.uuid4()
    path = tmp_path / str(input_id)
    path.write_bytes(b"normalized image")
    headers = {"Authorization": "Bearer private-test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        route = "/v1/image-inputs/purge"
        assert (await client.post(route, json={"input_id": str(input_id)})).status_code == 401
        assert path.exists()
        assert (
            await client.post(route, json={"input_id": "../outside"}, headers=headers)
        ).status_code == 422
        assert (
            await client.post(
                route, json={"input_id": str(input_id), "path": "/etc/passwd"}, headers=headers
            )
        ).status_code == 422
        app.state.worker.recipe_active = True
        assert (
            await client.post(route, json={"input_id": str(input_id)}, headers=headers)
        ).status_code == 429
        assert path.exists()
        app.state.worker.recipe_active = False
        for _ in range(2):
            response = await client.post(route, json={"input_id": str(input_id)}, headers=headers)
            assert response.status_code == 200
            assert response.json() == {
                "schema_version": 1,
                "input_id": str(input_id),
                "purged": True,
            }
            assert not path.exists()
        outside = tmp_path / "outside"
        outside.write_bytes(b"keep")
        path.symlink_to(outside)
        assert (
            await client.post(route, json={"input_id": str(input_id)}, headers=headers)
        ).status_code == 503
        assert path.is_symlink() and outside.read_bytes() == b"keep"
        path.unlink()
        os.link(outside, path)
        assert (
            await client.post(route, json={"input_id": str(input_id)}, headers=headers)
        ).status_code == 503
        assert path.exists() and outside.read_bytes() == b"keep"
