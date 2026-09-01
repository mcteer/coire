from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from coire_node.run_relay import create_app


def relay() -> tuple[TestClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "x-secret": "no"},
            json={"ok": True},
        )

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TestClient(
        create_app("http://gateway.invalid/v1", max_request_bytes=64, client=upstream)
    ), seen


def test_relay_allows_only_fixed_v1_post_destinations_and_filters_headers() -> None:
    client, seen = relay()
    with client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer secret", "X-Attacker": "drop"},
            json={"model": "safe"},
        )
        assert response.status_code == 200
        assert client.post("/v1/models", json={}).status_code == 404
        assert client.get("/v1/chat/completions").status_code == 405
    assert str(seen[0].url) == "http://gateway.invalid/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer secret"
    assert "x-attacker" not in seen[0].headers
    assert response.headers["content-type"].startswith("application/json")
    assert "x-secret" not in response.headers


def test_relay_rejects_declared_and_observed_oversized_requests() -> None:
    client, seen = relay()
    with client:
        assert client.post("/v1/messages", content=b"x" * 65).status_code == 413
    assert seen == []
