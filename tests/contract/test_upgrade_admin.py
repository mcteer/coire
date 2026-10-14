"""Admin upgrade reads expose bounded public metadata and require live authority."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from upgrade_fixtures import core_plan, plan

from coire_api.auth import Principal, PrincipalKind
from coire_api.routes import admin_upgrades
from coire_api.upgrades.authorization import require_upgrade_admin
from coire_core.errors import CoireError, UpgradeForbidden
from coire_core.models.auth import UserRole
from coire_core.models.upgrades import UpgradeEventPage
from coire_core.settings import Settings


def client(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True, allowed: bool = True):
    app = FastAPI()
    app.state.settings = Settings(upgrades_enabled=enabled)
    app.include_router(admin_upgrades.router)

    async def admin():
        if not allowed:
            raise UpgradeForbidden()
        return Principal(kind=PrincipalKind.ADMIN, user_id=uuid4(), role=UserRole.ADMIN)

    async def problem(request, error):
        return JSONResponse(error.to_problem().model_dump(mode="json"), status_code=error.status)

    app.dependency_overrides[require_upgrade_admin] = admin
    app.add_exception_handler(CoireError, problem)
    session = AsyncMock()

    @asynccontextmanager
    async def sessions():
        yield session

    monkeypatch.setattr(admin_upgrades, "session_scope", sessions)
    authorize = AsyncMock()
    monkeypatch.setattr(admin_upgrades, "authorize", authorize)
    return TestClient(app), session, authorize


@pytest.mark.parametrize(
    "route",
    ["/releases", "/targets", "/01ARZ3NDEKTSV4RRFFQ69G5FAV", "/01ARZ3NDEKTSV4RRFFQ69G5FAV/events"],
)
def test_admin_reads_refuse_disabled_or_unauthorized(monkeypatch, route):
    for enabled, allowed, code in [(False, True, 503), (True, False, 403)]:
        api, session, authorize = client(monkeypatch, enabled=enabled, allowed=allowed)
        response = api.get("/api/v1/admin/upgrades" + route)
        assert response.status_code == code
        session.scalars.assert_not_awaited()
        authorize.assert_not_awaited()


def test_release_page_omits_executor_configuration(monkeypatch):
    value = plan()
    api, session, authorize = client(monkeypatch)
    session.scalars.return_value = SimpleNamespace(
        all=lambda: [
            SimpleNamespace(
                id=value.source.release_id, payload=value.source.model_dump(mode="json")
            )
        ]
    )
    response = api.get("/api/v1/admin/upgrades/releases?limit=1")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    item = response.json()["items"][0]
    assert item["manifest_sha256"] == value.source.manifest_sha256
    assert "manifest" not in item and "configuration" not in item
    authorize.assert_awaited_once()
    assert api.get("/api/v1/admin/upgrades/releases?limit=101").status_code == 422
    assert api.get("/api/v1/admin/upgrades/releases?cursor=../../private").status_code == 422


@pytest.mark.parametrize("created", [True, False])
def test_studio_start_returns_durable_location_and_reuses_replay(monkeypatch, created):
    from coire_api.upgrades import acceptance

    value = plan()
    api, _, _ = client(monkeypatch)
    accept = AsyncMock(return_value=(value.operation, created))
    monkeypatch.setattr(acceptance, "accept_studio", accept)
    body = {
        "release_id": str(value.target.release_id),
        "expected_source_sha256": value.source.manifest_sha256,
        "idempotency_key": str(value.operation.idempotency_key),
        "acknowledge_maintenance": True,
    }
    route = "/api/v1/admin/upgrades/nodes/coire-edge-a"
    response = api.post(route, json=body)
    assert response.status_code == (202 if created else 200)
    assert response.headers["Location"].endswith(value.operation.job_id)
    assert response.headers["Cache-Control"] == "no-store"
    assert api.post(route, json=body | {"command": "untrusted"}).status_code == 422
    assert api.post(route.replace("coire-edge-a", "coire-core"), json=body).status_code == 422
    assert api.post(route, json=body | {"acknowledge_maintenance": False}).status_code == 422
    accept.assert_awaited_once()


def test_durable_events_recheck_authority_and_bound_cursor(monkeypatch):
    value = plan()
    api, _, authorize = client(monkeypatch)
    page = AsyncMock(
        return_value=UpgradeEventPage(operation=value.operation, events=(), cursor=0, reset=False)
    )
    monkeypatch.setattr(admin_upgrades, "event_page", page)
    route = f"/api/v1/admin/upgrades/{value.operation.job_id}/events"
    response = api.get(route + "?after=0&limit=10")
    assert response.status_code == 200
    assert response.json()["operation"]["job_id"] == value.operation.job_id
    authorize.assert_awaited_once()
    assert page.await_args.kwargs == {"after": 0, "limit": 10}
    assert api.get(route + "?after=-1").status_code == 422
    assert api.get(route + "?limit=101").status_code == 422


@pytest.mark.parametrize("created", [True, False])
def test_rollback_route_reuses_identity_and_rejects_arbitrary_destination(monkeypatch, created):
    from coire_api.upgrades import acceptance

    value = plan()
    api, _, _ = client(monkeypatch)
    accept = AsyncMock(return_value=(value.operation, created))
    monkeypatch.setattr(acceptance, "accept_rollback", accept)
    body = {
        "expected_source_sha256": value.target.manifest_sha256,
        "idempotency_key": str(uuid4()),
        "acknowledge_maintenance": True,
    }
    route = f"/api/v1/admin/upgrades/{value.operation.job_id}/rollback"
    response = api.post(route, json=body)
    assert response.status_code == (202 if created else 200)
    assert response.headers["Location"].endswith(value.operation.job_id)
    assert response.headers["Cache-Control"] == "no-store"
    assert (
        api.post(route, json=body | {"release_id": str(value.target.release_id)}).status_code == 422
    )
    assert api.post(route, json=body | {"acknowledge_maintenance": False}).status_code == 422
    accept.assert_awaited_once()


def test_registered_admin_routes_reject_anonymous_before_catalog_reads(monkeypatch):
    from coire_api.app import create_app

    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    lookup = AsyncMock()
    monkeypatch.setattr(admin_upgrades, "get_operation", lookup)
    app = create_app(Settings(upgrades_enabled=True))
    api = TestClient(app)
    assert api.get("/api/v1/admin/upgrades/releases").status_code == 401
    assert api.get("/api/v1/admin/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAV").status_code == 401
    lookup.assert_not_awaited()


def test_event_stream_replays_cursor_and_stops_at_terminal(monkeypatch):
    from datetime import UTC, datetime

    from coire_core.models.upgrades import UpgradePhase

    value = plan()
    api, _, authorize = client(monkeypatch)
    terminal = value.operation.model_copy(
        update={
            "phase": UpgradePhase.CANCELLED_UNCHANGED,
            "finished_at": datetime.now(UTC),
            "receipt_sequence": 0,
        }
    )
    page = AsyncMock(
        return_value=UpgradeEventPage(operation=terminal, events=(), cursor=0, reset=True)
    )
    monkeypatch.setattr(admin_upgrades, "event_page", page)
    response = api.get(
        f"/api/v1/admin/upgrades/{terminal.job_id}/events",
        headers={"Accept": "text/event-stream", "Last-Event-ID": "99"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: reset" in response.text
    assert "cancelled_unchanged" in response.text
    assert page.await_args.kwargs["after"] == 99
    authorize.assert_awaited_once()


@pytest.mark.parametrize("cursor", ["-1", "letters", "9223372036854775808"])
def test_stream_rejects_invalid_last_event_id(monkeypatch, cursor):
    api, _, _ = client(monkeypatch)
    response = api.get(
        "/api/v1/admin/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAV/events",
        headers={"Last-Event-ID": cursor},
    )
    assert response.status_code == 422


def test_registration_resolves_only_trusted_prepared_store(monkeypatch):
    from pathlib import Path
    from unittest.mock import Mock

    value = plan()
    api, _, _ = client(monkeypatch)
    verify = Mock(return_value=value.target)
    register = AsyncMock(return_value=value.target)
    monkeypatch.setattr(admin_upgrades, "verify_prepared_release", verify)
    monkeypatch.setattr(admin_upgrades, "register_release", register)
    body = {
        "prepared_artifact_id": value.target.prepared_artifact_id,
        "expected_sha256": value.target.manifest_sha256,
    }
    response = api.post("/api/v1/admin/upgrades/releases", json=body)
    assert response.status_code == 201
    assert response.json()["manifest_sha256"] == value.target.manifest_sha256
    assert verify.call_args.args[0] == Path("/opt/coire/upgrades/prepared")
    assert verify.call_args.args[1].model_dump(mode="json") == body
    register.assert_awaited_once()
    assert (
        api.post("/api/v1/admin/upgrades/releases", json=body | {"path": "/private"}).status_code
        == 422
    )
    assert verify.call_count == 1


def test_sse_revalidates_live_authority_before_more_progress(monkeypatch):
    value = plan()
    api, _, authorize = client(monkeypatch)
    authorize.side_effect = [None, UpgradeForbidden()]
    page = AsyncMock(
        return_value=UpgradeEventPage(operation=value.operation, events=(), cursor=0, reset=False)
    )
    monkeypatch.setattr(admin_upgrades, "event_page", page)
    response = api.get(
        f"/api/v1/admin/upgrades/{value.operation.job_id}/events",
        headers={"Accept": "text/event-stream"},
    )
    assert response.status_code == 200
    assert response.text.count("event: snapshot") == 1
    assert authorize.await_count == 2
    page.assert_awaited_once()


@pytest.mark.parametrize("created", [True, False])
def test_core_start_is_typed_and_replays_the_same_accepted_job(monkeypatch, created):
    from coire_api.upgrades import acceptance

    value = core_plan()
    api, _, _ = client(monkeypatch)
    accepted = AsyncMock(return_value=(value.operation, created))
    monkeypatch.setattr(acceptance, "accept_core", accepted)
    body = {
        "release_id": str(value.target.release_id),
        "expected_source_sha256": value.source.manifest_sha256,
        "idempotency_key": str(value.operation.idempotency_key),
        "acknowledge_maintenance": True,
    }
    response = api.post("/api/v1/admin/upgrades/control-plane", json=body)
    assert response.status_code == (202 if created else 200)
    assert response.headers["Location"].endswith(value.operation.job_id)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["kind"] == "control_plane"
    assert (
        api.post(
            "/api/v1/admin/upgrades/control-plane", json=body | {"command": "docker pull"}
        ).status_code
        == 422
    )
    assert (
        api.post(
            "/api/v1/admin/upgrades/control-plane", json=body | {"acknowledge_maintenance": False}
        ).status_code
        == 422
    )
    accepted.assert_awaited_once()


@pytest.mark.parametrize("enabled,allowed,expected", [(False, True, 503), (True, False, 403)])
def test_executor_handover_route_refuses_before_acceptance(monkeypatch, enabled, allowed, expected):
    from coire_api.upgrades import handover_authority

    accepted = AsyncMock()
    monkeypatch.setattr(handover_authority, "start_handover", accepted, raising=False)
    api, _, _ = client(monkeypatch, enabled=enabled, allowed=allowed)
    response = api.post(
        "/api/v1/admin/upgrades/executor-handover",
        json={
            "release_id": str(uuid4()),
            "expected_incumbent_seed_sha256": "a" * 64,
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == expected
    accepted.assert_not_awaited()


@pytest.mark.parametrize("created", [False, True])
def test_executor_handover_acceptance_returns_only_bounded_summary(monkeypatch, created):
    from datetime import UTC, datetime, timedelta

    from coire_api.upgrades import handover_authority
    from coire_core.models.upgrades import CoreHandoverSummary

    now = datetime.now(UTC)
    summary = CoreHandoverSummary(
        handover_id=uuid4(),
        project="coire",
        generation=2,
        phase="accepted",
        preparation_sha256="a" * 64,
        candidate_release_id=uuid4(),
        incumbent_image_id="sha256:" + "b" * 64,
        candidate_image_id="sha256:" + "c" * 64,
        prepared_at=now,
        deadline_at=now + timedelta(seconds=120),
    )
    accepted = AsyncMock(return_value=(summary, created))
    monkeypatch.setattr(handover_authority, "start_handover", accepted)
    api, _, _ = client(monkeypatch)
    body = {
        "release_id": str(summary.candidate_release_id),
        "expected_incumbent_seed_sha256": "a" * 64,
        "idempotency_key": str(uuid4()),
    }
    response = api.post("/api/v1/admin/upgrades/executor-handover", json=body)
    assert response.status_code == (202 if created else 200)
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == summary.model_dump(mode="json")
    for extra in ({"command": "docker exec"}, {"candidate_runtime_id": "caller-owned"}):
        assert (
            api.post("/api/v1/admin/upgrades/executor-handover", json=body | extra).status_code
            == 422
        )
    accepted.assert_awaited_once()


def test_executor_handover_status_requires_authority_and_hides_missing_project(monkeypatch):
    identity = uuid4()
    api, session, authorized = client(monkeypatch)
    session.get.return_value = None
    response = api.get(f"/api/v1/admin/upgrades/executor-handover/{identity}")
    assert response.status_code == 404
    authorized.assert_awaited_once()
    session.get.return_value = SimpleNamespace(project="other")
    assert api.get(f"/api/v1/admin/upgrades/executor-handover/{identity}").status_code == 404
    assert api.get("/api/v1/admin/upgrades/executor-handover/invalid").status_code == 422
    api, session, authorized = client(monkeypatch, allowed=False)
    assert api.get(f"/api/v1/admin/upgrades/executor-handover/{identity}").status_code == 403
    session.get.assert_not_awaited()
