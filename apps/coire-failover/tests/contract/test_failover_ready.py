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
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Coire-Failover-Monitor": "isolated-monitor"},
    ) as client:
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
    private, _, membership = _HELPERS._cluster()
    access, key = _HELPERS._access_client()
    model = _HELPERS._model()
    app = create_app(
        FailoverRuntime(
            load_snapshot=lambda: _HELPERS._snapshot(private, membership, model),
            load_lease=lambda: None,
            access_client=access,
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/", headers={"Cf-Access-Jwt-Assertion": _HELPERS._token(key)})
    await access.aclose()
    assert response.status_code == 200
    assert 'data-coire-tier="failover"' in response.text


@pytest.mark.parametrize(
    "path",
    ["/", "/assets/main.js", "/ready", "/failover/tier", "/v1/models", "/v1/chat/completions"],
)
@pytest.mark.parametrize("credential", [None, "browser", "monitor", "lifecycle"])
async def test_every_route_enforces_its_identity_scope(
    path: str,
    credential: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "main.js").write_text("void 0;", encoding="utf-8")
    (tmp_path / "index.html").write_text("<html>isolated frontend</html>", encoding="utf-8")
    monkeypatch.setattr(failover_app, "_STATIC_ROOT", tmp_path)
    private, _, membership = _HELPERS._cluster()
    access, key = _HELPERS._access_client()
    model = _HELPERS._model()
    runtime = FailoverRuntime(
        load_snapshot=lambda: _HELPERS._snapshot(private, membership, model),
        load_lease=lambda: None,
        access_client=access,
    )
    runtime.in_flight = 3
    headers = {
        None: {},
        "browser": {"Cf-Access-Jwt-Assertion": _HELPERS._token(key)},
        "monitor": {"X-Coire-Failover-Monitor": "isolated-monitor"},
        "lifecycle": {"X-Coire-Failover-Lifecycle": "isolated-lifecycle"},
    }[credential]
    async with AsyncClient(
        transport=ASGITransport(app=create_app(runtime)), base_url="http://direct-origin"
    ) as client:
        response = await client.request(
            "POST" if path == "/v1/chat/completions" else "GET",
            path,
            headers=headers,
            json={"model": str(model.id), "messages": [{"role": "user", "content": "hi"}]},
        )
    await access.aclose()
    expected = 401
    if credential == "monitor" and path == "/ready":
        expected = 503
    elif credential == "lifecycle" and path == "/failover/tier":
        expected = 200
        assert response.json()["in_flight"] == 3
    elif credential == "browser" and path != "/ready":
        expected = 503 if path.startswith("/v1/") else 200
        if path == "/failover/tier":
            assert "in_flight" not in response.json()
            assert response.json()["admission_available"] is False
    assert response.status_code == expected
    assert response.headers["cache-control"] == "no-store"


async def test_valid_access_subject_not_approved_by_snapshot_is_refused() -> None:
    private, _, membership = _HELPERS._cluster()
    model = _HELPERS._model()
    access, key = _HELPERS._access_client()
    runtime = FailoverRuntime(
        load_snapshot=lambda: _HELPERS._snapshot(private, membership, model),
        load_lease=lambda: _HELPERS._lease(private, datetime.now(UTC)),
        access_client=access,
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app(runtime)), base_url="http://direct-origin"
    ) as client:
        response = await client.get(
            "/v1/models",
            headers={"Cf-Access-Jwt-Assertion": _HELPERS._token(key, sub="unapproved")},
        )
    await access.aclose()
    assert response.status_code == 401
