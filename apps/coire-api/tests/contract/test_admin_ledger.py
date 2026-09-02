from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.app import create_app
from coire_api.placement import service
from coire_core.models.placement import PinUpdate, ReservationHolder
from coire_core.settings import Settings


def test_placement_admin_contract_is_typed_and_guarded() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    paths = document["paths"]
    expected = {
        "/api/v1/admin/ledger": "get",
        "/api/v1/admin/ledger/{node_id}": "patch",
        "/api/v1/admin/ledger/reservations/{reservation_id}": "patch",
        "/api/v1/admin/models/{model_id}/placement": "post",
        "/api/v1/admin/placements/{decision_id}": "get",
    }
    for path, method in expected.items():
        operation = paths[path][method]
        assert operation["security"] == [{"HTTPBearer": []}]
        assert "422" in operation["responses"]


def test_placement_submission_has_typed_accepted_response() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    operation = document["paths"]["/api/v1/admin/models/{model_id}/placement"]["post"]
    accepted = operation["responses"]["202"]["content"]["application/json"]["schema"]
    assert accepted["$ref"].endswith("/PlacementDecision")
    schemas = document["components"]["schemas"]
    assert schemas["PlacementRequest"]["additionalProperties"] is False
    assert schemas["PlacementDecision"]["additionalProperties"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("pinned", "action"), [(True, "model.pin"), (False, "model.unpin")])
async def test_pin_mutation_writes_matching_audit(
    monkeypatch: pytest.MonkeyPatch, pinned: bool, action: str
) -> None:
    reservation = SimpleNamespace(
        holder_type=ReservationHolder.MODEL,
        holder_id=str(uuid.uuid4()),
        pinned=not pinned,
    )
    audits: list[str] = []

    class Session:
        async def get(self, _model: object, _id: object) -> object:
            return reservation

    async def audit(_session: object, **values: object) -> None:
        audits.append(str(values["action"]))

    monkeypatch.setattr(service, "write_audit", audit)
    await service.set_pin(
        cast(AsyncSession, Session()),
        uuid.uuid4(),
        PinUpdate(pinned=pinned),
        actor="operator",
    )
    assert reservation.pinned is pinned
    assert audits == [action]
