"""External targets must be curated and audited before any provider call is possible."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.app import create_app
from coire_api.db import ModelRow, ModelStateTransitionRow
from coire_api.registry import service
from coire_api.routes import admin_models
from coire_core.models.registry import (
    ModelSource,
    ModelState,
    ModelUpdateRequest,
    ProviderModelAddRequest,
    Visibility,
)
from coire_core.settings import Settings


def _request(**changes: object) -> ProviderModelAddRequest:
    values: dict[str, object] = {
        "source": "openai",
        "provider_model_id": "gpt-example",
        "display_name": "Example frontier model",
        "context_window": 8192,
        "max_output_tokens": 512,
        "daily_token_budget": 10_000,
    }
    values.update(changes)
    return ProviderModelAddRequest.model_validate(values)


async def test_acquired_studio_model_cannot_publish_without_default_variant() -> None:
    class Session:
        async def scalar(self, query: object) -> uuid.UUID | None:
            if "is_default" in str(query):
                return None
            return uuid.uuid4()

    model = ModelRow(
        id=uuid.uuid4(),
        slug="tiny@4bit",
        source="studio",
        state=ModelState.READY,
        visibility=Visibility.ADMIN_ONLY,
    )
    with pytest.raises(service.RegistryError, match="validated default variant"):
        await service.update_model(
            cast(AsyncSession, Session()),
            model,
            ModelUpdateRequest(visibility=Visibility.PUBLISHED),
            actor="operator",
        )
    assert model.visibility is Visibility.ADMIN_ONLY


def test_provider_registration_is_strict_and_admin_guarded() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    operation = document["paths"]["/api/v1/admin/provider-models"]["post"]
    assert operation["security"] == [{"HTTPBearer": []}]
    assert operation["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/Model"
    )
    assert (
        document["components"]["schemas"]["ProviderModelAddRequest"]["additionalProperties"]
        is False
    )
    for invalid in (
        {"source": "studio"},
        {"provider_model_id": "../../other"},
        {"endpoint": "https://attacker.invalid"},
        {"max_output_tokens": 0},
    ):
        with pytest.raises(ValidationError):
            _request(**invalid)


@pytest.mark.asyncio
async def test_provider_registration_writes_audit_and_starts_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[object] = []
    audits: list[dict[str, Any]] = []

    class Session:
        committed = False

        async def scalar(self, _query: object) -> None:
            return None

        def add(self, row: object) -> None:
            rows.append(row)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            self.committed = True

    async def audit(_session: object, **values: Any) -> None:
        audits.append(values)

    monkeypatch.setattr(admin_models, "write_principal_audit", audit)
    session = Session()
    model = await admin_models.add_provider_model(
        _request(), cast(Any, SimpleNamespace(subject="operator")), cast(AsyncSession, session)
    )
    assert isinstance(model, ModelRow)
    assert model.source == ModelSource.OPENAI
    assert model.visibility is Visibility.ADMIN_ONLY
    assert model.state.value == "ready"
    assert model.provider_model_id == "gpt-example"
    assert any(isinstance(row, ModelStateTransitionRow) for row in rows)
    assert audits[0]["action"] == "provider_model.add"
    assert audits[0]["target_id"] == str(model.id)
    assert session.committed

    with pytest.raises(service.RegistryError, match="routing is not enabled"):
        await service.update_model(
            cast(AsyncSession, session),
            model,
            ModelUpdateRequest(visibility=Visibility.PUBLISHED),
            actor="operator",
        )
    monkeypatch.setattr(service, "write_audit", audit)
    await service.update_model(
        cast(AsyncSession, session),
        model,
        ModelUpdateRequest(visibility=Visibility.PUBLISHED),
        actor="operator",
        provider_ready=True,
    )
    assert model.visibility is Visibility.PUBLISHED
    assert audits[-1]["action"] == "model.publish"
