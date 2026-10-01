from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import MemoryReservationRow, ModelInstanceRow
from coire_api.placement import service
from coire_api.placement.service import LedgerNotFoundError
from coire_api.routes import admin_ledger
from coire_core.errors import ImageForbidden
from coire_core.models.instance import InstanceState
from coire_core.models.placement import MemoryReservationState, PinUpdate, ReservationHolder
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
@pytest.mark.parametrize(
    ("holder", "pinned", "action"),
    [
        (ReservationHolder.MODEL, True, "model.pin"),
        (ReservationHolder.MODEL, False, "model.unpin"),
        (ReservationHolder.IMAGE, True, "image.worker.pin"),
        (ReservationHolder.IMAGE, False, "image.worker.unpin"),
    ],
)
async def test_pin_mutation_writes_matching_audit(
    monkeypatch: pytest.MonkeyPatch, holder: ReservationHolder, pinned: bool, action: str
) -> None:
    reservation = SimpleNamespace(
        holder_type=holder,
        holder_id=str(uuid.uuid4()),
        node_id=uuid.uuid4(),
        state=MemoryReservationState.HELD,
        pinned=not pinned,
    )
    audits: list[str] = []

    class Session:
        async def get(self, model: object, _id: object, **kwargs: object) -> object:
            if model is ModelInstanceRow:
                return SimpleNamespace(state=InstanceState.READY)
            assert model is MemoryReservationRow
            return reservation

        async def execute(self, statement: object, parameters: object = None) -> None:
            pass

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


async def test_draining_image_worker_cannot_be_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    reservation = SimpleNamespace(
        holder_type=ReservationHolder.IMAGE,
        holder_id=str(uuid.uuid4()),
        node_id=uuid.uuid4(),
        state=MemoryReservationState.HELD,
        pinned=False,
    )

    class Session:
        async def get(self, model: object, _id: object, **kwargs: object) -> object:
            if model is ModelInstanceRow:
                return SimpleNamespace(state=InstanceState.DRAINING)
            return reservation

        async def execute(self, statement: object, parameters: object = None) -> None:
            pass

    with pytest.raises(LedgerNotFoundError):
        await service.set_pin(
            cast(AsyncSession, Session()),
            uuid.uuid4(),
            PinUpdate(pinned=True),
            actor="operator",
        )
    assert reservation.pinned is False


async def test_image_pin_route_requires_live_human_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_calls: list[str] = []

    class Session:
        async def get(self, model: object, identity: object) -> object:
            assert model is MemoryReservationRow
            return SimpleNamespace(holder_type=ReservationHolder.IMAGE)

    async def deny(request: Request, principal: Principal) -> Principal:
        guard_calls.append(principal.kind.value)
        raise ImageForbidden()

    async def pin(*args: object, **kwargs: object) -> None:
        pytest.fail("unauthorized image pin reached ledger mutation")

    monkeypatch.setattr(admin_ledger, "require_human_image_admin", deny)
    monkeypatch.setattr(service, "set_pin", pin)
    request = Request({"type": "http", "method": "PATCH", "path": "/", "headers": []})
    with pytest.raises(ImageForbidden):
        await admin_ledger.patch_reservation(
            uuid.uuid4(),
            PinUpdate(pinned=True),
            request,
            Principal(kind=PrincipalKind.SERVICE, scopes=frozenset({"admin"})),
            cast(AsyncSession, Session()),
        )
    assert guard_calls == ["service"]
