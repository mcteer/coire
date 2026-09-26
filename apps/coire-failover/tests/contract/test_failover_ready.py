"""Election-gated readiness fails closed for a stale or missing proof."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from coire_core.models.failover import FailoverLease
from coire_failover import app as failover_app
from coire_failover.app import FailoverRuntime, create_app

_SPEC = importlib.util.spec_from_file_location(
    "failover_gateway_contract",
    Path(__file__).with_name("test_failover_gateway.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
_HELPERS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPERS)


@pytest.mark.asyncio
async def test_ready_is_success_only_for_a_current_proof() -> None:
    private, _, membership = _HELPERS._cluster()
    model = _HELPERS._model()
    now = datetime.now(UTC)
    current = _HELPERS._lease(private, now)
    runtime = FailoverRuntime(
        load_snapshot=lambda: _HELPERS._snapshot(private, membership, model),
        load_lease=lambda: current,
    )
    app = create_app(runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/ready")).status_code == 200
        runtime.load_lease = lambda: None
        assert (await client.get("/ready")).status_code == 503
        proof = current.proof
        assert proof is not None
        expired = current.model_copy(
            update={
                "proof": proof.model_copy(
                    update={
                        "grants": [
                            proof.grants[0].model_copy(
                                update={"expires_at": now - timedelta(seconds=1)}
                            ),
                            proof.grants[1],
                        ]
                    }
                )
            }
        )
        runtime.load_lease = lambda: expired
        assert (await client.get("/ready")).status_code == 503


def test_a_lease_without_authority_is_rejected() -> None:
    with pytest.raises(ValueError):
        FailoverLease(holder="coire-edge-a")


@pytest.mark.asyncio
async def test_root_serves_the_degraded_app_without_a_redirect_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "index.html").write_text('<html lang="en"><head></head><body>Coire</body></html>')
    monkeypatch.setattr(failover_app, "_STATIC_ROOT", tmp_path)
    app = create_app(FailoverRuntime())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert 'data-coire-tier="failover"' in response.text
