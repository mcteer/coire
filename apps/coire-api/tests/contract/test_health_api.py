"""Contract tests for /health and /ready (T020).

Responses are validated against `specs/000-bootstrap/contracts/health-api.yaml` itself, not
against a hand-copied shape, so drift between the implementation and the reviewed contract
fails here rather than in a consumer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from coire_api.deps import SessionDep, SettingsDep  # noqa: F401  (imported for override keys)
from coire_api.routes import health
from coire_core.models.health import ServiceHealth
from coire_core.settings import Settings

CONTRACT = Path(__file__).resolve().parents[4] / "specs/000-bootstrap/contracts/health-api.yaml"


@pytest.fixture(scope="session")
def contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(CONTRACT.read_text())
    return loaded


def validator_for(contract: dict[str, Any], schema_name: str) -> Draft202012Validator:
    """Build a validator for one component schema, with $refs resolvable."""
    schema = {
        **contract["components"]["schemas"][schema_name],
        "$defs": contract["components"]["schemas"],
    }
    # Rewrite contract-style refs to local $defs.
    text = yaml.dump(schema)
    text = text.replace("#/components/schemas/", "#/$defs/")
    return Draft202012Validator(yaml.safe_load(text))


class FakeResult:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


class FakeSession:
    """Minimal async session: succeeds, or raises to simulate Postgres being down."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def execute(self, *_: Any, **__: Any) -> FakeResult:
        if self.fail:
            raise ConnectionRefusedError("postgres is down")
        return FakeResult([])


def build_app(*, db_fails: bool = False, settings: Settings | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(health.router)

    async def _session() -> AsyncIterator[FakeSession]:
        yield FakeSession(fail=db_fails)

    from coire_api.db import get_session
    from coire_core.settings import get_settings

    app.dependency_overrides[get_session] = _session
    configured = settings or Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    app.dependency_overrides[get_settings] = lambda: configured
    return app


async def call(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_ready_matches_contract(contract: dict[str, Any]) -> None:
    resp = await call(build_app(), "/ready")
    assert resp.status_code == 200
    validator_for(contract, "ReadyResponse").validate(resp.json())
    assert resp.json()["ready"] is True


async def test_health_matches_contract_when_dependencies_are_down(
    contract: dict[str, Any],
) -> None:
    """Nothing is reachable in a unit test, so this is the degraded/unhealthy shape."""
    resp = await call(build_app(), "/health")
    validator_for(contract, "HealthResponse").validate(resp.json())


async def test_health_is_503_and_unhealthy_when_postgres_fails(contract: dict[str, Any]) -> None:
    """Postgres is the only critical dependency: without it there is no system of record."""
    resp = await call(build_app(db_fails=True), "/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    validator_for(contract, "HealthResponse").validate(body)
    pg = next(s for s in body["services"] if s["name"] == "postgres")
    assert pg["healthy"] is False
    assert pg["detail"]


async def test_health_reports_every_dependency() -> None:
    body = (await call(build_app(), "/health")).json()
    assert {s["name"] for s in body["services"]} == {
        "postgres",
        "mcp",
        "scheduler",
        "otel-collector",
    }


async def test_health_records_latency_for_each_probe() -> None:
    body = (await call(build_app(), "/health")).json()
    assert all(s["latency_ms"] is not None for s in body["services"])


async def test_configured_tunnel_probe_emits_live_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations: list[tuple[int, dict[str, str]]] = []

    class Gauge:
        def set(self, value: int, attributes: dict[str, str]) -> None:
            observations.append((value, attributes))

    original = health._probe_http

    async def probe(client: httpx.AsyncClient, name: str, url: str) -> Any:
        if name == "tunnel":
            return ServiceHealth(
                name=name, healthy=True, checked_at=datetime.now(UTC), latency_ms=1
            )
        return await original(client, name, url)

    monkeypatch.setattr(health, "_tunnel_up", Gauge())
    monkeypatch.setattr(health, "_probe_http", probe)
    response = await call(
        build_app(settings=Settings(tunnel_health_url="http://cloudflared:2000/ready")),
        "/health",
    )
    assert response.status_code == 200
    assert observations == [(1, {"tunnel": "primary"})]


async def test_probes_run_concurrently_not_serially() -> None:
    """Three unreachable HTTP probes at a 2s timeout must not take 6s (spec US2)."""
    import time

    started = time.perf_counter()
    await call(build_app(), "/health")
    elapsed = time.perf_counter() - started
    assert elapsed < health.PROBE_TIMEOUT_S * 2, f"probes appear serial: {elapsed:.2f}s"
