"""Preset mutations require a live human admin and commit an audited revision."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from coire_api.auth import Principal, PrincipalKind, require_admin
from coire_api.db import ApiKeyRow, UserRow, get_session
from coire_api.images import admin_presets, coexistence
from coire_api.routes import admin_images
from coire_core.errors import CoireError
from coire_core.models.auth import UserRole
from coire_core.models.images import ImageCoexistenceProfile, ImagePreset, ImageSubmitRequest
from coire_core.settings import Settings

OWNER = uuid.uuid4()
ORIGIN = "https://coire.example.test"
PRESET = uuid.uuid4()
MODEL = uuid.uuid4()
KEY = uuid.uuid4()


class FakeSession:
    def __init__(
        self,
        *,
        role: UserRole = UserRole.ADMIN,
        active: bool = True,
        key_scopes: list[str] | None = None,
    ) -> None:
        self.role = role
        self.active = active
        self.key_scopes = key_scopes if key_scopes is not None else ["admin", "images"]
        self.commits = 0
        self.rollbacks = 0

    async def get(self, model: type[object], identity: object, **kwargs: object) -> Any:
        assert kwargs.get("with_for_update") is True
        if model is UserRow:
            assert identity == OWNER
            return UserRow(id=OWNER, role=self.role, active=self.active)
        assert model is ApiKeyRow and identity == KEY
        return ApiKeyRow(id=KEY, user_id=OWNER, scopes=self.key_scopes)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _app(
    principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
    session: FakeSession,
    audits: list[dict[str, object]],
) -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(_secrets_dir="/nonexistent", chat_browser_origin=ORIGIN)  # type: ignore[call-arg]
    app.include_router(admin_images.router)
    app.include_router(admin_images.coexistence_router)
    app.dependency_overrides[require_admin] = lambda: principal

    async def route_session():  # type: ignore[no-untyped-def]
        yield session

    @asynccontextmanager
    async def audit_scope():  # type: ignore[no-untyped-def]
        yield session

    async def audit(db: object, **kwargs: object) -> None:
        audits.append(kwargs)

    app.dependency_overrides[get_session] = route_session
    monkeypatch.setattr(admin_images, "session_scope", audit_scope)
    monkeypatch.setattr(admin_images, "write_audit", audit)

    @app.exception_handler(CoireError)
    async def problem(request: Request, exc: CoireError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content=exc.to_problem().model_dump(mode="json")
        )

    return app


def _human() -> Principal:
    return Principal(kind=PrincipalKind.ADMIN, user_id=OWNER, role=UserRole.ADMIN)


def _scoped_key(*, scopes: frozenset[str] = frozenset({"admin", "images"})) -> Principal:
    return Principal(
        kind=PrincipalKind.API_KEY,
        user_id=OWNER,
        role=UserRole.ADMIN,
        scopes=scopes,
        api_key_id=KEY,
        credential_version=1,
    )


@pytest.mark.parametrize(
    "principal,role,key_scopes,expected",
    [
        (_scoped_key(), UserRole.ADMIN, ["admin", "images"], 201),
        (_scoped_key(), UserRole.USER, ["admin", "images"], 403),
        (_scoped_key(), UserRole.ADMIN, ["images"], 403),
        (_scoped_key(scopes=frozenset({"admin"})), UserRole.ADMIN, ["admin"], 403),
    ],
)
async def test_coexistence_accepts_only_live_scoped_admin_key(
    principal: Principal,
    role: UserRole,
    key_scopes: list[str],
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, object]] = []
    session = FakeSession(role=role, key_scopes=key_scopes)

    async def live(db: object, caller: Principal, **kwargs: object) -> uuid.UUID:
        assert caller is principal
        if "images" not in caller.scopes:
            from coire_core.errors import ImageForbidden

            raise ImageForbidden()
        return OWNER

    async def approve(db: object, caller: Principal, body: object) -> ImageCoexistenceProfile:
        assert caller is principal
        return ImageCoexistenceProfile.model_validate(
            {
                "id": str(uuid.uuid4()),
                "profile_hash": "a" * 64,
                "status": "approved",
                "report": body,
            }
        )

    monkeypatch.setattr(admin_images, "authorize_live_image_action", live)
    monkeypatch.setattr(coexistence, "admit_coexistence_report", approve)
    app = _app(principal, monkeypatch, session, audits)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.post(
            "/api/v1/admin/image-coexistence-profiles",
            json={
                "node_id": str(uuid.uuid4()),
                "image_model_id": str(MODEL),
                "chat_variant_ids": [str(uuid.uuid4())],
                "hardware_fingerprint": "a" * 64,
                "runtime_fingerprint": "b" * 64,
                "measured_bounds": {
                    "max_width": 512,
                    "max_height": 512,
                    "max_steps": 4,
                    "max_outputs": 1,
                },
                "duration_seconds": 900,
                "prompt_tokens_max": 4096,
                "first_token_p95_ms": 1000,
                "gateway_overhead_p95_ms": 10,
                "image_completed_count": 1,
                "image_progress_observed": True,
                "swap_observed": False,
                "thermal_alarm": False,
                "runtime_version": "mflux-0.20.0",
                "measured_at": "2026-10-03T00:00:00Z",
                "valid_until": "2026-10-04T00:00:00Z",
            },
        )
    assert response.status_code == expected
    assert session.commits == (1 if expected == 201 else 0)
    assert len(audits) == (0 if expected == 201 else 1)


def _body() -> dict[str, object]:
    return {
        "name": "Portrait",
        "prompt_prefix": "studio",
        "defaults": {"model_id": str(MODEL), "prompt": "subject"},
    }


@pytest.mark.parametrize(
    "principal",
    [
        Principal(kind=PrincipalKind.API_KEY, user_id=OWNER, scopes=frozenset({"admin", "images"})),
        Principal(kind=PrincipalKind.OPS_SERVICE),
        Principal(kind=PrincipalKind.RUN, user_id=OWNER),
        Principal(kind=PrincipalKind.ADMIN),
    ],
)
async def test_nonhuman_admin_credentials_are_refused_and_audited(
    principal: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    audits: list[dict[str, object]] = []
    session = FakeSession()
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal, monkeypatch, session, audits)),
        base_url=ORIGIN,
    ) as client:
        response = await client.post(
            "/api/v1/admin/image-presets", json=_body(), headers={"Origin": ORIGIN}
        )
    assert response.status_code == 403
    assert len(audits) == 1 and audits[0]["action"] == "image.preset.admin_refused"
    assert session.commits == 0


@pytest.mark.parametrize(
    "origin, role, active",
    [
        (None, UserRole.ADMIN, True),
        (ORIGIN, UserRole.USER, True),
        (ORIGIN, UserRole.ADMIN, False),
    ],
)
async def test_wrong_origin_or_demoted_admin_is_refused(
    origin: str | None, role: UserRole, active: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    audits: list[dict[str, object]] = []
    session = FakeSession(role=role, active=active)
    headers = {"Origin": origin} if origin else {}
    async with AsyncClient(
        transport=ASGITransport(app=_app(_human(), monkeypatch, session, audits)),
        base_url=ORIGIN,
    ) as client:
        response = await client.post("/api/v1/admin/image-presets", json=_body(), headers=headers)
    assert response.status_code == 403
    assert len(audits) == 1 and session.commits == 0


async def test_human_admin_create_commits_and_routes_are_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, object]] = []
    session = FakeSession()
    calls: list[tuple[object, dict[str, object]]] = []

    async def live(db: object, principal: Principal, **kwargs: object) -> uuid.UUID:
        return OWNER

    async def create(db: object, body: object, **kwargs: object) -> ImagePreset:
        calls.append((body, kwargs))
        return ImagePreset(
            id=PRESET,
            revision=1,
            name="Portrait",
            prompt_prefix="studio",
            defaults=ImageSubmitRequest(model_id=MODEL, prompt="subject"),
        )

    monkeypatch.setattr(admin_images, "authorize_live_image_action", live)
    monkeypatch.setattr(admin_presets, "create_image_preset", create)
    app = _app(_human(), monkeypatch, session, audits)
    document = app.openapi()
    assert "post" in document["paths"]["/api/v1/admin/image-presets"]
    assert {"patch", "delete"} <= document["paths"][
        "/api/v1/admin/image-presets/{preset_id}"
    ].keys()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.post(
            "/api/v1/admin/image-presets", json=_body(), headers={"Origin": ORIGIN}
        )
    assert response.status_code == 201
    assert response.json()["id"] == str(PRESET)
    assert len(calls) == 1 and calls[0][1]["admin_user_id"] == OWNER
    assert session.commits == 1


async def test_update_and_retire_commit_but_stale_edit_audits_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, object]] = []
    session = FakeSession()

    async def live(db: object, principal: Principal, **kwargs: object) -> uuid.UUID:
        return OWNER

    async def update(
        db: object, preset_id: uuid.UUID, body: object, **kwargs: object
    ) -> ImagePreset:
        assert preset_id == PRESET
        return ImagePreset(
            id=PRESET,
            revision=2,
            name="Portrait",
            defaults=ImageSubmitRequest(model_id=MODEL, prompt="subject"),
        )

    async def retire(db: object, preset_id: uuid.UUID, **kwargs: object) -> None:
        assert preset_id == PRESET

    monkeypatch.setattr(admin_images, "authorize_live_image_action", live)
    monkeypatch.setattr(admin_presets, "update_image_preset", update)
    monkeypatch.setattr(admin_presets, "retire_image_preset", retire)
    app = _app(_human(), monkeypatch, session, audits)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        changed = await client.patch(
            f"/api/v1/admin/image-presets/{PRESET}",
            json={"expected_revision": 1, "name": "Portrait"},
            headers={"Origin": ORIGIN},
        )
        retired = await client.delete(
            f"/api/v1/admin/image-presets/{PRESET}", headers={"Origin": ORIGIN}
        )
    assert changed.status_code == 200 and changed.json()["revision"] == 2
    assert retired.status_code == 204 and session.commits == 2

    async def stale(
        db: object, preset_id: uuid.UUID, body: object, **kwargs: object
    ) -> ImagePreset:
        from coire_core.errors import ImageConflict

        raise ImageConflict()

    monkeypatch.setattr(admin_presets, "update_image_preset", stale)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        conflict = await client.patch(
            f"/api/v1/admin/image-presets/{PRESET}",
            json={"expected_revision": 1},
            headers={"Origin": ORIGIN},
        )
    assert conflict.status_code == 409
    assert session.rollbacks == 1 and len(audits) == 1


async def test_duplicate_preset_name_rolls_back_and_audits_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, object]] = []
    session = FakeSession()

    async def live(db: object, principal: Principal, **kwargs: object) -> uuid.UUID:
        return OWNER

    async def duplicate(db: object, body: object, **kwargs: object) -> ImagePreset:
        raise IntegrityError("insert image_presets", {}, Exception("duplicate"))

    monkeypatch.setattr(admin_images, "authorize_live_image_action", live)
    monkeypatch.setattr(admin_presets, "create_image_preset", duplicate)
    async with AsyncClient(
        transport=ASGITransport(app=_app(_human(), monkeypatch, session, audits)),
        base_url=ORIGIN,
    ) as client:
        response = await client.post(
            "/api/v1/admin/image-presets", json=_body(), headers={"Origin": ORIGIN}
        )
    assert response.status_code == 409
    assert response.json()["coire_code"] == "image_conflict"
    assert session.rollbacks == 1 and session.commits == 0
    assert len(audits) == 1 and audits[0]["action"] == "image.preset.admin_refused"
