"""Visual admin acquisition fails closed on the disposable Linux node topology."""

from __future__ import annotations

import os
import time

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run the local visual acquisition test",
    ),
]

TINY_VISUAL_REPO = "mlx-community/SmolVLM-256M-Instruct-4bit"
PINNED_REVISION = "69cb5195f414ceb6398c5581254673d2c6c8d0d8"


def test_admin_acquired_visual_source_cannot_publish_without_native_smoke(
    api_url: str, admin_headers: dict[str, str]
) -> None:
    """Linux nodes transfer the real tiny model, then refuse to claim it was validated."""
    with httpx.Client(base_url=api_url, timeout=60.0) as client:
        response = client.post(
            "/api/v1/admin/models/acquisitions",
            headers=admin_headers,
            json={
                "repo_id": TINY_VISUAL_REPO,
                "revision": PINNED_REVISION,
                "variant": {"name": "upstream-4bit", "precision": "4bit"},
            },
        )
        assert response.status_code in (200, 202), response.text
        workflow = response.json()
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            current = client.get(
                f"/api/v1/admin/acquisitions/{workflow['id']}", headers=admin_headers
            )
            current.raise_for_status()
            workflow = current.json()
            if workflow["state"] in ("failed", "succeeded"):
                break
            time.sleep(2)
        assert workflow["state"] == "failed", workflow
        stages = {stage["stage"]: stage["status"] for stage in workflow["stages"]}
        assert stages["pull"] == "succeeded"
        assert stages["validate"] == "failed"
        variants = client.get(
            f"/api/v1/admin/models/{workflow['model_id']}/variants", headers=admin_headers
        )
        variants.raise_for_status()
        variant = next(
            item for item in variants.json() if item["id"] == workflow["variant_id"]
        )
        assert variant["validated"] is False
        assert variant["state"] != "ready"
