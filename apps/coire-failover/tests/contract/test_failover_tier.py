"""Tier projection names every capability the degraded surface refuses."""

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.routing import Route

from coire_failover.app import create_app
from coire_failover.tier import MINIMAL_UNAVAILABLE, project_tier


def test_tiers_enumerate_unavailable_capabilities() -> None:
    full = project_tier(
        elected_host="coire-core",
        reachable=frozenset({"coire-core", "coire-edge-a", "coire-edge-b"}),
        full=True,
    )
    degraded = project_tier(
        elected_host="coire-edge-a",
        reachable=frozenset({"coire-edge-a", "coire-edge-b"}),
    )
    minimal = project_tier(elected_host="coire-edge-b", reachable=frozenset({"coire-edge-b"}))
    assert full.tier.value == "full"
    assert full.unavailable_capabilities == ()
    assert degraded.tier.value == "degraded_inference"
    assert "conversation_persistence" in degraded.unavailable_capabilities
    assert "admin" in degraded.unavailable_capabilities
    assert "sharded_inference" not in degraded.unavailable_capabilities
    assert minimal.tier.value == "minimal"
    assert "sharded_inference" in minimal.unavailable_capabilities
    assert set(MINIMAL_UNAVAILABLE) >= set(degraded.unavailable_capabilities)


@pytest.mark.asyncio
async def test_mutation_routes_are_absent_and_tier_is_explicit() -> None:
    app = create_app()
    paths = {route.path for route in app.routes if isinstance(route, Route)}
    for forbidden in ("/api/v1/admin/models", "/api/v1/runs", "/mcp", "/v1/images"):
        assert forbidden not in paths
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        tier = await client.get("/failover/tier")
    body = tier.json()
    assert body["tier"] == "minimal"
    assert "conversation_persistence" in body["unavailable_capabilities"]
    assert "model_acquisition" in body["unavailable_capabilities"]
