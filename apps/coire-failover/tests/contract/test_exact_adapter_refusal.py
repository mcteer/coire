"""Adapter selectors stop at the elected inference edge before residency or relay I/O."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from coire_core.models.failover import FailoverSnapshot
from coire_core.settings import Settings
from coire_failover import app as frontend
from coire_failover.tier import project_tier


async def test_pair_selector_refused_before_resident_slug_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = MagicMock(spec=FailoverSnapshot)
    snapshot.is_fresh.return_value = True
    local, peer, relay = AsyncMock(), AsyncMock(), AsyncMock()
    runtime = frontend.FailoverRuntime(
        settings=Settings(),
        load_snapshot=lambda: snapshot,
        load_lease=lambda: None,
        local_resident=local,
        peer_resident=peer,
        relay=relay,
    )
    monkeypatch.setattr(frontend, "_identity", AsyncMock())
    monkeypatch.setattr(frontend, "_lease_serves", lambda *args: True)
    monkeypatch.setattr(
        frontend,
        "_status_for",
        lambda *args: project_tier(elected_host=None, reachable=frozenset()),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=frontend.create_app(runtime)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": f"{uuid.uuid4()}@adapter",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 503 and response.json()["reason"] == "adapter_unavailable"
    local.assert_not_called()
    peer.assert_not_called()
    relay.assert_not_called()
