"""Only measured, current chat/image pairs may enter the coexistence ledger."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ImageCoexistenceProfileRow, ModelRow, ModelVariantRow, NodeRow, get_session
from coire_api.images import coexistence
from coire_api.routes import admin_images
from coire_core.errors import ImageValidationError
from coire_core.models.acquisition import VariantState
from coire_core.models.images import (
    ImageCoexistenceBounds,
    ImageCoexistenceProfile,
    ImageCoexistenceReportRequest,
)
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.registry import EngineBackend, ModelKind, ModelSource, ModelState, Visibility
from coire_scheduler.image_admission import (
    chat_mix_allowed,
    node_hardware_fingerprint,
    node_runtime_fingerprint,
)

NODE = uuid.uuid4()
IMAGE = uuid.uuid4()
CHAT = uuid.uuid4()
CHAT_MODEL = uuid.uuid4()
NODE_FACTS = SimpleNamespace(
    name="coire-edge-b",
    role=NodeRole.STUDIO,
    reachability=Reachability.HEALTHY,
    memory_total_bytes=128 * 1024**3,
    gpu_cores=40,
    agent_version="0.2.0",
)


def _report() -> ImageCoexistenceReportRequest:
    now = datetime.now(UTC)
    return ImageCoexistenceReportRequest(
        node_id=NODE,
        image_model_id=IMAGE,
        chat_variant_ids=(CHAT,),
        hardware_fingerprint=node_hardware_fingerprint(cast(NodeRow, NODE_FACTS)),
        runtime_fingerprint=node_runtime_fingerprint(cast(NodeRow, NODE_FACTS)),
        measured_bounds=ImageCoexistenceBounds(
            max_width=512, max_height=512, max_steps=4, max_outputs=1
        ),
        duration_seconds=900,
        prompt_tokens_max=4096,
        first_token_p95_ms=1499.0,
        gateway_overhead_p95_ms=19.0,
        image_completed_count=2,
        image_progress_observed=True,
        swap_observed=False,
        thermal_alarm=False,
        runtime_version="mflux-0.20.0",
        measured_at=now - timedelta(minutes=5),
        valid_until=now + timedelta(days=1),
    )


class _Scalars:
    def __init__(self, row: ImageCoexistenceProfileRow) -> None:
        self.row = row

    def all(self) -> list[ImageCoexistenceProfileRow]:
        return [self.row]


class _Session:
    def __init__(self) -> None:
        self.node = SimpleNamespace(**vars(NODE_FACTS))
        self.image = SimpleNamespace(
            kind=ModelKind.IMAGE_MODEL,
            backend=EngineBackend.MFLUX,
            source=ModelSource.STUDIO,
            state=ModelState.READY,
            visibility=Visibility.PUBLISHED,
            image_capability_profile={
                "modes": ["txt2img"],
                "min_width": 512,
                "max_width": 512,
                "min_height": 512,
                "max_height": 512,
                "max_pixels": 512 * 512,
                "min_steps": 4,
                "max_steps": 4,
                "min_guidance": 0,
                "max_guidance": 0,
                "max_outputs": 1,
            },
        )
        self.variant = SimpleNamespace(
            model_id=CHAT_MODEL,
            backend=EngineBackend.MLX_LM.value,
            state=VariantState.READY,
            validated=True,
            published=True,
        )
        self.chat_model = SimpleNamespace(kind=ModelKind.LANGUAGE_MODEL, state=ModelState.READY)
        self.row: ImageCoexistenceProfileRow | None = None

    async def get(self, model: type[object], identity: object, **kwargs: object) -> object:
        if model is ImageCoexistenceProfileRow:
            assert self.row is not None and identity == self.row.id
            return self.row
        if model is NodeRow:
            assert identity == NODE and kwargs.get("populate_existing") is True
            return self.node
        if model is ModelVariantRow:
            assert identity == CHAT and kwargs.get("with_for_update") is True
            return self.variant
        assert model is ModelRow
        return self.image if identity == IMAGE else self.chat_model

    def add(self, row: ImageCoexistenceProfileRow) -> None:
        self.row = row

    async def flush(self) -> None:
        pass

    async def scalars(self, statement: object) -> _Scalars:
        assert self.row is not None
        return _Scalars(self.row)


async def test_admin_report_is_audited_and_dispatch_admits_measured_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    audited: list[dict[str, object]] = []

    async def audit(session: object, **kwargs: object) -> None:
        audited.append(kwargs)

    monkeypatch.setattr(coexistence, "write_audit", audit)
    principal = Principal(kind=PrincipalKind.ADMIN, user_id=uuid.uuid4())
    approved = await coexistence.admit_coexistence_report(
        cast(AsyncSession, session), principal, _report()
    )
    assert approved.status == "approved"
    assert approved.report.image_completed_count == 2
    assert len(audited) == 1 and audited[0]["action"] == "image.coexistence.approved"
    assert await chat_mix_allowed(
        cast(AsyncSession, session), NODE, IMAGE, {str(CHAT)}, datetime.now(UTC)
    )
    assert not await chat_mix_allowed(
        cast(AsyncSession, session), NODE, IMAGE, {str(uuid.uuid4())}, datetime.now(UTC)
    )
    invalidated = await coexistence.invalidate_coexistence_profile(
        cast(AsyncSession, session), principal, approved.id
    )
    assert invalidated.status == "invalidated"
    assert len(audited) == 2 and audited[1]["action"] == "image.coexistence.invalidated"
    assert not await chat_mix_allowed(
        cast(AsyncSession, session), NODE, IMAGE, {str(CHAT)}, datetime.now(UTC)
    )


async def test_report_refuses_stale_node_variant_and_unmeasured_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def audit(session: object, **kwargs: object) -> None:
        pytest.fail("invalid report must not be approved")

    monkeypatch.setattr(coexistence, "write_audit", audit)
    principal = Principal(kind=PrincipalKind.ADMIN, user_id=uuid.uuid4())
    session = _Session()
    with pytest.raises(ImageValidationError, match="stale"):
        await coexistence.admit_coexistence_report(
            cast(AsyncSession, session),
            principal,
            _report().model_copy(update={"measured_at": datetime.now(UTC) - timedelta(days=2)}),
        )
    session.node.reachability = Reachability.DEGRADED
    with pytest.raises(ImageValidationError, match="node unavailable"):
        await coexistence.admit_coexistence_report(
            cast(AsyncSession, session), principal, _report()
        )
    session.node.reachability = Reachability.HEALTHY
    session.variant.validated = False
    with pytest.raises(ImageValidationError, match="variant unavailable"):
        await coexistence.admit_coexistence_report(
            cast(AsyncSession, session), principal, _report()
        )
    session.variant.validated = True
    oversized = _report().model_copy(
        update={
            "measured_bounds": ImageCoexistenceBounds(
                max_width=1024, max_height=512, max_steps=4, max_outputs=1
            )
        }
    )
    with pytest.raises(ImageValidationError, match="bounds exceed"):
        await coexistence.admit_coexistence_report(
            cast(AsyncSession, session), principal, oversized
        )


def test_report_contract_refuses_failing_benchmark() -> None:
    base = _report().model_dump(mode="json")
    for key, value in (
        ("duration_seconds", 899),
        ("first_token_p95_ms", 1500.1),
        ("gateway_overhead_p95_ms", 20.1),
        ("image_completed_count", 0),
        ("image_progress_observed", False),
        ("swap_observed", True),
        ("thermal_alarm", True),
        ("runtime_version", "mflux-0.19.0"),
    ):
        with pytest.raises(ValidationError):
            ImageCoexistenceReportRequest.model_validate({**base, key: value})


async def test_admin_coexistence_route_uses_typed_report_and_no_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report()
    principal = Principal(kind=PrincipalKind.ADMIN, user_id=uuid.uuid4())
    app = FastAPI()
    app.include_router(admin_images.coexistence_router)
    app.dependency_overrides[admin_images.require_human_image_admin] = lambda: principal
    committed = 0

    class Session:
        async def commit(self) -> None:
            nonlocal committed
            committed += 1

    session = Session()

    async def fake_session() -> AsyncIterator[Session]:
        yield session

    app.dependency_overrides[get_session] = fake_session
    calls: list[ImageCoexistenceReportRequest] = []

    async def approve(
        received_session: object,
        received_principal: Principal,
        received_report: ImageCoexistenceReportRequest,
    ) -> ImageCoexistenceProfile:
        assert received_session is session and received_principal is principal
        calls.append(received_report)
        return ImageCoexistenceProfile(
            id=uuid.uuid4(),
            profile_hash="c" * 64,
            status="approved",
            report=received_report,
        )

    monkeypatch.setattr(coexistence, "admit_coexistence_report", approve)
    invalidated_calls: list[uuid.UUID] = []

    async def invalidate(
        received_session: object, received_principal: Principal, profile_id: uuid.UUID
    ) -> ImageCoexistenceProfile:
        assert received_session is session and received_principal is principal
        invalidated_calls.append(profile_id)
        return ImageCoexistenceProfile(
            id=profile_id,
            profile_hash="c" * 64,
            status="invalidated",
            report=report,
            invalidated_at=datetime.now(UTC),
        )

    monkeypatch.setattr(coexistence, "invalidate_coexistence_profile", invalidate)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://coire.test"
    ) as client:
        valid = await client.post(
            "/api/v1/admin/image-coexistence-profiles", json=report.model_dump(mode="json")
        )
        invalid = await client.post(
            "/api/v1/admin/image-coexistence-profiles",
            json={**report.model_dump(mode="json"), "duration_seconds": 1},
        )
        removed = await client.delete(
            f"/api/v1/admin/image-coexistence-profiles/{valid.json()['id']}"
        )
    assert valid.status_code == 201
    assert valid.headers["cache-control"] == "private, no-store"
    assert valid.json()["status"] == "approved"
    assert invalid.status_code == 422
    assert removed.status_code == 200 and removed.json()["status"] == "invalidated"
    assert invalidated_calls == [uuid.UUID(valid.json()["id"])]
    assert calls == [report] and committed == 2
