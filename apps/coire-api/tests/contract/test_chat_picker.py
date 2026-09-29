"""Native Chat picker and owner-derived draft creation contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.db import EngineProcessRow, ModelRow, get_session
from coire_core.models.engine import EngineState
from coire_core.models.registry import ModelState, Visibility
from coire_core.settings import Settings

TOKEN = "chat-picker-contract"
NOW = datetime.now(UTC)


def _model(**changes: object) -> ModelRow:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "repo_id": "private/repository",
        "slug": "private--repository",
        "display_name": "Readable model",
        "description": "Good for general questions",
        "state": ModelState.READY,
        "visibility": Visibility.PUBLISHED,
        "entitlement": ["team-a"],
        "tags": ["general"],
        "precision": "4bit",
        "weight_bytes": 4 * 1024**3,
        "memory_estimate_bytes": 6 * 1024**3,
        "context_window": 4096,
        "capability_profile": {"verified": True},
        "backend": "mlx_lm",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return ModelRow(**values)


class _Session:
    def __init__(
        self, models: list[ModelRow], engines: list[EngineProcessRow] | None = None
    ) -> None:
        self.models = models
        self.engines = engines or []
        self.created: list[object] = []
        self.committed = False

    async def execute(self, statement: object) -> object:
        rows = self.engines if "engine_processes" in str(statement) else self.models
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def get(self, model: object, identifier: uuid.UUID) -> ModelRow | None:
        return next((row for row in self.models if row.id == identifier), None)

    async def scalars(self, statement: object) -> object:
        eligible = [row.id for row in self.models if "coding" in row.tags]
        if "harness_verified" in str(statement):
            eligible = [
                row.id
                for row in self.models
                if "coding" in row.tags and row.capability_profile.get("verified")
            ]
        return SimpleNamespace(all=lambda: eligible)

    def add(self, row: object) -> None:
        self.created.append(row)

    async def flush(self) -> None:
        return None

    async def refresh(self, row: object) -> None:
        row.created_at = NOW  # type: ignore[attr-defined]
        row.updated_at = NOW  # type: ignore[attr-defined]

    async def commit(self) -> None:
        self.committed = True


def _app(principal: Principal, session: _Session) -> FastAPI:
    settings = Settings(
        _secrets_dir="/nonexistent",
        chat_browser_origin="http://localhost",
        chat_enabled=True,
        identity_legacy_admin_enabled=True,
        admin_token=SecretStr(TOKEN),
    )  # type: ignore[call-arg]
    app = create_app(settings)
    app.dependency_overrides[require_principal] = lambda: principal

    async def fake_session():  # type: ignore[no-untyped-def]
        yield session

    app.dependency_overrides[get_session] = fake_session
    return app


async def _request(
    app: FastAPI, method: str, path: str, *, json: object | None = None, origin: str | None = None
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if origin is not None:
        headers["Origin"] = origin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        return await client.request(method, path, json=json, headers=headers)


async def test_picker_is_entitled_ready_published_and_safe_for_admin() -> None:
    visible = _model()
    hidden = _model(visibility=Visibility.ADMIN_ONLY, entitlement=[])
    unready = _model(state=ModelState.DOWNLOADING, entitlement=[])
    engine = EngineProcessRow(
        id=uuid.uuid4(),
        model_id=visible.id,
        node_id=uuid.uuid4(),
        state=EngineState.READY,
        started_at=NOW,
        load_seconds=12.0,
    )
    session = _Session([visible, hidden, unready], [engine])
    user_id = uuid.uuid4()
    for kind in (PrincipalKind.USER, PrincipalKind.ADMIN):
        principal = Principal(kind=kind, user_id=user_id, entitlements=frozenset({"team-a"}))
        response = await _request(_app(principal, session), "GET", "/api/v1/chat/models")
        assert response.status_code == 200
        assert len(response.json()["data"]) == 1
        entry = response.json()["data"][0]
        assert entry["id"] == str(visible.id)
        assert entry["load_state"] == "loaded"
        assert entry["estimated_warmup_seconds"] == 12.0
        assert entry["verified"] is True
        assert not {"repo_id", "slug", "node_id", "port", "model_path"} & entry.keys()
    unentitled = Principal(kind=PrincipalKind.USER, user_id=user_id)
    assert (await _request(_app(unentitled, session), "GET", "/api/v1/chat/models")).json() == {
        "data": []
    }


async def test_code_picker_filters_profile_and_verified_apply_variant() -> None:
    principal = Principal(
        kind=PrincipalKind.USER, user_id=uuid.uuid4(), entitlements=frozenset({"team-a"})
    )
    general = _model(tags=["general"])
    read_code = _model(tags=["coding"], capability_profile={"verified": False})
    write_code = _model(tags=["coding"], capability_profile={"verified": True})
    remote_code = _model(tags=["coding"], source="openai", capability_profile={"verified": True})
    app = _app(principal, _Session([general, read_code, write_code, remote_code]))
    chat = await _request(app, "GET", "/api/v1/chat/models?mode=chat")
    remote_entry = next(row for row in chat.json()["data"] if row["id"] == str(remote_code.id))
    assert remote_entry["source"] == "openai"
    assert remote_entry["load_state"] == "loaded"
    research = await _request(app, "GET", "/api/v1/chat/models?mode=code&action=research")
    assert {row["id"] for row in research.json()["data"]} == {str(read_code.id), str(write_code.id)}
    apply = await _request(app, "GET", "/api/v1/chat/models?mode=code&action=apply")
    assert {row["id"] for row in apply.json()["data"]} == {str(write_code.id)}


async def test_create_derives_owner_and_refuses_cross_origin_or_ineligible_model() -> None:
    user_id = uuid.uuid4()
    principal = Principal(
        kind=PrincipalKind.USER, user_id=user_id, entitlements=frozenset({"team-a"})
    )
    eligible = _model()
    hidden = _model(visibility=Visibility.ADMIN_ONLY)
    session = _Session([eligible, hidden])
    app = _app(principal, session)
    body = {"title": "My notes", "mode": "chat", "model_id": str(eligible.id)}
    assert (await _request(app, "POST", "/api/v1/chat/conversations", json=body)).status_code == 403
    bad = await _request(
        app,
        "POST",
        "/api/v1/chat/conversations",
        json={**body, "owner_id": str(uuid.uuid4())},
        origin="http://localhost",
    )
    assert bad.status_code == 422
    denied = await _request(
        app,
        "POST",
        "/api/v1/chat/conversations",
        json={**body, "model_id": str(hidden.id)},
        origin="http://localhost",
    )
    assert denied.status_code == 404
    assert denied.headers["content-type"].startswith("application/problem+json")
    created = await _request(
        app, "POST", "/api/v1/chat/conversations", json=body, origin="http://localhost"
    )
    assert created.status_code == 201
    assert created.json()["owner_id"] == str(user_id)
    assert created.json()["revision"] == 1
    assert created.json()["selected_model_id"] == str(eligible.id)
    assert session.committed


async def test_empty_picker_never_creates_model_or_job() -> None:
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    session = _Session([])
    response = await _request(_app(principal, session), "GET", "/api/v1/chat/models")
    assert response.status_code == 200
    assert response.json() == {"data": []}
    assert session.created == []


async def test_visual_capability_requires_measured_verification() -> None:
    unverified = _model(
        tags=["coding"],
        capability_profile={"verified": True},
        backend="mlx_vlm",
        visual_capability={
            "verified": False,
            "max_images": 2,
            "max_image_pixels": 1_000_000,
            "max_encoded_bytes": 1_000_000,
        },
    )
    verified = _model(
        tags=["coding"],
        capability_profile={"verified": True},
        backend="mlx_vlm",
        visual_capability={
            "verified": True,
            "max_images": 2,
            "max_image_pixels": 1_000_000,
            "max_encoded_bytes": 1_000_000,
        },
    )
    principal = Principal(
        kind=PrincipalKind.USER, user_id=uuid.uuid4(), entitlements=frozenset({"team-a"})
    )
    response = await _request(
        _app(principal, _Session([unverified, verified])),
        "GET",
        "/api/v1/chat/models?mode=code&action=apply",
    )
    assert response.status_code == 200
    entries = {entry["id"]: entry for entry in response.json()["data"]}
    assert entries[str(unverified.id)]["accepts_images"] is False
    assert entries[str(unverified.id)]["max_images"] is None
    assert entries[str(verified.id)]["accepts_images"] is True
    assert entries[str(verified.id)]["max_images"] == 2


async def test_chat_routes_are_disabled_by_default() -> None:
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    app = _app(principal, _Session([]))
    app.state.settings.chat_enabled = False
    response = await _request(app, "GET", "/api/v1/chat/models")
    assert response.status_code == 404
