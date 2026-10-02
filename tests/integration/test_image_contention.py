"""Simulated chat arrival and uncertain image residency share one admission decision."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from coire_api.db import ModelRow, ModelVariantRow, NodeMemoryLedgerRow, NodeRow
from coire_core.models.acquisition import VariantState
from coire_core.models.images import ImageCoexistenceBounds, ImageCoexistenceReportRequest
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.registry import ModelKind, ModelState
from coire_scheduler.image_admission import (
    chat_mix_allowed,
    coexistence_report_hash,
    image_available_bytes,
    node_hardware_fingerprint,
    node_runtime_fingerprint,
)
from coire_scheduler.image_dispatch import ImageNodeCandidate, choose_image_node

pytestmark = pytest.mark.integration
NODE = uuid.uuid4()
IMAGE = uuid.uuid4()
CHAT = uuid.uuid4()
OTHER_CHAT = uuid.uuid4()
NOW = datetime.now(UTC)


class _Rows:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def all(self) -> list[object]:
        return self.rows


class _Session:
    def __init__(self) -> None:
        self.node = SimpleNamespace(
            id=NODE,
            name="coire-edge-b",
            role=NodeRole.STUDIO,
            reachability=Reachability.HEALTHY,
            memory_total_bytes=100,
            gpu_cores=40,
            agent_version="0.2.0",
        )
        self.ledger = SimpleNamespace(budget_bytes=100, measured_resident_bytes=20)
        self.holds: list[object] = [
            SimpleNamespace(
                bytes=20,
                holder_type=ReservationHolder.MODEL,
                holder_id=str(CHAT),
                state=MemoryReservationState.HELD,
            )
        ]
        self.workers: list[uuid.UUID] = []
        self.profile = self._approved_profile()

    def _approved_profile(self) -> SimpleNamespace:
        report = ImageCoexistenceReportRequest(
            node_id=NODE,
            image_model_id=IMAGE,
            chat_variant_ids=(CHAT,),
            hardware_fingerprint=node_hardware_fingerprint(cast(NodeRow, self.node)),
            runtime_fingerprint=node_runtime_fingerprint(cast(NodeRow, self.node)),
            measured_bounds=ImageCoexistenceBounds(
                max_width=512, max_height=512, max_steps=4, max_outputs=1
            ),
            duration_seconds=900,
            prompt_tokens_max=4096,
            first_token_p95_ms=1200,
            gateway_overhead_p95_ms=15,
            image_completed_count=1,
            image_progress_observed=True,
            swap_observed=False,
            thermal_alarm=False,
            runtime_version="mflux-0.20.0",
            measured_at=NOW - timedelta(minutes=5),
            valid_until=NOW + timedelta(hours=1),
        )
        return SimpleNamespace(
            status="approved",
            invalidated_at=None,
            valid_until=report.valid_until,
            node_id=NODE,
            image_model_id=IMAGE,
            chat_variant_ids=[str(CHAT)],
            hardware_fingerprint=report.hardware_fingerprint,
            runtime_fingerprint=report.runtime_fingerprint,
            image_mode="txt2img",
            measured_bounds=report.measured_bounds.model_dump(mode="json"),
            first_token_p95_ms=report.first_token_p95_ms,
            benchmark_result=report.model_dump(mode="json"),
            profile_hash=coexistence_report_hash(report),
        )

    async def get(self, model: type[object], identity: object, **kwargs: object) -> object:
        del kwargs
        if model is NodeRow:
            return self.node
        if model is NodeMemoryLedgerRow:
            return self.ledger
        if model is ModelVariantRow:
            return SimpleNamespace(
                model_id=CHAT,
                state=VariantState.READY,
                validated=True,
                published=True,
            )
        assert model is ModelRow
        return SimpleNamespace(kind=ModelKind.LANGUAGE_MODEL, state=ModelState.READY)

    async def scalars(self, statement: object) -> _Rows:
        query = str(statement)
        if "image_coexistence_profiles" in query:
            return _Rows([self.profile])
        if "memory_reservations" in query:
            return _Rows(self.holds)
        assert "model_instances" in query
        return _Rows(list(self.workers))


async def _candidate(session: _Session, residents: set[str]) -> ImageNodeCandidate:
    allowed = await chat_mix_allowed(cast(Any, session), NODE, IMAGE, residents, NOW)
    available = await image_available_bytes(
        cast(Any, session), cast(Any, session.node), IMAGE, 40, budget_fraction=0.9
    )
    return ImageNodeCandidate(
        name="coire-edge-b",
        node_id=NODE,
        healthy=True,
        memory_total_bytes=available,
        image_busy=False,
        chat_unmeasured=not allowed,
    )


async def test_chat_arrival_and_changed_profile_hold_image_dispatch() -> None:
    session = _Session()
    initial = await _candidate(session, {str(CHAT)})
    assert choose_image_node("pinned:coire-edge-b", 40, [initial]) == initial

    admission_lock = asyncio.Lock()
    chat_arrived = asyncio.Event()
    residents = {str(CHAT)}

    async def admit_chat() -> None:
        async with admission_lock:
            residents.add(str(OTHER_CHAT))
            chat_arrived.set()

    async def attempt_image() -> ImageNodeCandidate | None:
        await chat_arrived.wait()
        async with admission_lock:
            candidate = await _candidate(session, residents)
            return choose_image_node("pinned:coire-edge-b", 40, [candidate])

    chat, image = await asyncio.gather(admit_chat(), attempt_image())
    assert chat is None and image is None
    assert residents == {str(CHAT), str(OTHER_CHAT)}

    session.profile.invalidated_at = NOW
    assert choose_image_node("single:auto", 40, [await _candidate(session, {str(CHAT)})]) is None
    session.profile.invalidated_at = None
    session.node.agent_version = "0.3.0"
    assert choose_image_node("single:auto", 40, [await _candidate(session, {str(CHAT)})]) is None


async def test_uncertain_image_process_keeps_hold_after_worker_inventory_loss() -> None:
    session = _Session()
    instance_id = uuid.uuid4()
    session.holds.append(
        SimpleNamespace(
            bytes=40,
            holder_type=ReservationHolder.IMAGE,
            holder_id=str(instance_id),
            state=MemoryReservationState.HELD,
        )
    )
    session.ledger.measured_resident_bytes = 60
    session.workers = [instance_id]
    assert (await _candidate(session, {str(CHAT)})).memory_total_bytes == 70
    session.workers = []  # node lost its process inventory; no stop proof exists
    assert (await _candidate(session, {str(CHAT)})).memory_total_bytes == 0
