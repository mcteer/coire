"""A selected Chat model can turn cold without losing eligibility or its measured estimate."""

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

NOW = datetime.now(UTC)


class Session:
    def __init__(self, model: ModelRow, engines: list[EngineProcessRow]) -> None:
        self.model = model
        self.engines = engines
        self.created: list[object] = []

    async def execute(self, statement: object) -> object:
        rows = self.engines if "engine_processes" in str(statement) else [self.model]
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def get(self, _model: object, identifier: uuid.UUID) -> ModelRow | None:
        return self.model if identifier == self.model.id else None

    def add(self, row: object) -> None:
        self.created.append(row)

    async def flush(self) -> None:
        return None

    async def refresh(self, row: object) -> None:
        row.created_at = NOW  # type: ignore[attr-defined]
        row.updated_at = NOW  # type: ignore[attr-defined]

    async def commit(self) -> None:
        return None


def app_for(session: Session) -> FastAPI:
    settings = Settings(
        _secrets_dir="/nonexistent",
        chat_browser_origin="http://localhost",
        chat_enabled=True,
        identity_legacy_admin_enabled=True,
        admin_token=SecretStr("test"),
    )  # type: ignore[call-arg]
    app = create_app(settings)
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    app.dependency_overrides[require_principal] = lambda: principal

    async def fake_session():  # type: ignore[no-untyped-def]
        yield session

    app.dependency_overrides[get_session] = fake_session
    return app


def model() -> ModelRow:
    return ModelRow(
        id=uuid.uuid4(),
        repo_id="private/model",
        slug="private--model",
        display_name="Selected model",
        state=ModelState.READY,
        visibility=Visibility.PUBLISHED,
        entitlement=[],
        tags=["general"],
        precision="4bit",
        weight_bytes=1024,
        memory_estimate_bytes=2048,
        context_window=4096,
        capability_profile={"verified": True},
        backend="mlx_lm",
        created_at=NOW,
        updated_at=NOW,
    )


async def test_picker_preserves_model_and_measured_estimate_after_eviction() -> None:
    selected = model()
    engine = EngineProcessRow(
        id=uuid.uuid4(),
        model_id=selected.id,
        node_id=uuid.uuid4(),
        state=EngineState.READY,
        started_at=NOW,
        load_seconds=17.25,
    )
    session = Session(selected, [engine])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(session)), base_url="http://localhost"
    ) as client:
        loaded = await client.get("/api/v1/chat/models", headers={"Authorization": "Bearer test"})
        assert loaded.status_code == 200
        assert loaded.json()["data"][0]["load_state"] == "loaded"
        engine.state = EngineState.STOPPED
        cold = await client.get("/api/v1/chat/models", headers={"Authorization": "Bearer test"})
    assert cold.status_code == 200
    assert cold.json()["data"] == [{**loaded.json()["data"][0], "load_state": "cold"}]
    assert cold.json()["data"][0]["estimated_warmup_seconds"] == 17.25
    assert session.created == []


async def test_picker_unknown_estimate_stays_null_while_engine_starts() -> None:
    selected = model()
    engine = EngineProcessRow(
        id=uuid.uuid4(),
        model_id=selected.id,
        node_id=uuid.uuid4(),
        state=EngineState.STARTING,
        started_at=NOW,
    )
    session = Session(selected, [engine])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(session)), base_url="http://localhost"
    ) as client:
        response = await client.get("/api/v1/chat/models", headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["load_state"] == "loading"
    assert entry["estimated_warmup_seconds"] is None
    assert not {"queue_rank", "progress_percent", "eta_seconds"} & entry.keys()
