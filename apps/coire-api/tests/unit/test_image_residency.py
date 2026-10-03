"""Idle image unload preserves uncertain holds and honors human pinning."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import MemoryReservationRow, ModelInstanceRow, NodeRow
from coire_core.models.image_worker import ImageWorkerLoadResult
from coire_core.models.instance import InstanceState
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.settings import Settings
from coire_scheduler import image_residency

NODE = uuid.uuid4()
INSTANCE = uuid.uuid4()
RESERVATION = uuid.uuid4()
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_image_idle_ttl_waits_for_complete_interval() -> None:
    assert not image_residency.image_idle_due(
        NOW - timedelta(seconds=899), now=NOW, ttl_seconds=900
    )
    assert image_residency.image_idle_due(NOW - timedelta(seconds=900), now=NOW, ttl_seconds=900)


async def test_idle_prepare_pins_and_persists_drain_before_node_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reservation = SimpleNamespace(
        id=RESERVATION,
        node_id=NODE,
        holder_type=ReservationHolder.IMAGE,
        holder_id=str(INSTANCE),
        state=MemoryReservationState.HELD,
        pinned=False,
        last_used_at=NOW - timedelta(minutes=20),
    )
    instance = SimpleNamespace(
        id=INSTANCE,
        policy="image:coire-edge-b",
        state=InstanceState.READY,
        updated_at=NOW - timedelta(minutes=20),
        transitioned_at=NOW - timedelta(minutes=20),
    )
    audits: list[str] = []

    class Session:
        async def get(self, model: type[object], identity: object, **kwargs: object) -> object:
            if model is MemoryReservationRow:
                return reservation
            if model is ModelInstanceRow:
                return instance
            assert model is NodeRow and identity == NODE
            return SimpleNamespace(id=NODE, name="coire-edge-b")

        async def execute(self, statement: object, parameters: object = None) -> None:
            pass

        async def scalar(self, statement: object) -> object | None:
            selected = str(statement.selected_columns)  # type: ignore[attr-defined]
            if "instance_members" in selected:
                return SimpleNamespace(instance_id=INSTANCE, node_id=NODE)
            return None

    session = Session()

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, session)

    async def audit(session: object, **kwargs: object) -> None:
        audits.append(cast(str, kwargs["action"]))

    monkeypatch.setattr(image_residency, "session_scope", scope)
    monkeypatch.setattr(image_residency, "write_audit", audit)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert await image_residency._prepare_idle_unload(RESERVATION, settings, NOW) == (
        "coire-edge-b",
        NODE,
        INSTANCE,
    )
    assert instance.state is InstanceState.DRAINING
    assert audits == ["image.worker.idle_unload_requested"]
    assert await image_residency._prepare_idle_unload(RESERVATION, settings, NOW) == (
        "coire-edge-b",
        NODE,
        INSTANCE,
    )
    assert len(audits) == 1
    reservation.pinned = True
    assert await image_residency._prepare_idle_unload(RESERVATION, settings, NOW) is None


@pytest.mark.parametrize("proof", [False, True])
async def test_idle_sweep_releases_only_after_exact_node_stop_proof(
    monkeypatch: pytest.MonkeyPatch, proof: bool
) -> None:
    calls: list[str] = []

    class Rows:
        def all(self) -> list[uuid.UUID]:
            return [RESERVATION]

    class Session:
        async def scalars(self, statement: object) -> Rows:
            return Rows()

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, Session())

    async def prepare(*args: object) -> tuple[str, uuid.UUID, uuid.UUID]:
        calls.append("drain")
        return "coire-edge-b", NODE, INSTANCE

    async def confirm(*args: object) -> bool:
        calls.append("release")
        return True

    class Client:
        def __init__(self, settings: Settings) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def unload_image_worker(self, node: str, request: object) -> ImageWorkerLoadResult:
            calls.append("node")
            return ImageWorkerLoadResult(
                instance_id=INSTANCE,
                state="failed",
                reserved_bytes=0 if proof else 100,
                safe_error="worker_stopped" if proof else "uncertain",
            )

    monkeypatch.setattr(image_residency, "session_scope", scope)
    monkeypatch.setattr(image_residency, "_prepare_idle_unload", prepare)
    monkeypatch.setattr(image_residency, "_confirm_idle_unload", confirm)
    monkeypatch.setattr(image_residency, "NodeClient", Client)
    result = await image_residency.sweep_idle_image_workers(
        Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    )
    assert result == int(proof)
    assert calls == (["drain", "node", "release"] if proof else ["drain", "node"])


async def test_idle_sweep_advances_past_busy_first_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [uuid.UUID(int=value) for value in range(1, 27)]
    visited: list[uuid.UUID] = []

    class Rows:
        def __init__(self, values: list[uuid.UUID]) -> None:
            self.values = values

        def all(self) -> list[uuid.UUID]:
            return self.values

    class Session:
        async def scalars(self, statement: object) -> Rows:
            parameters = statement.compile().params  # type: ignore[attr-defined]
            after = next(
                (value for value in parameters.values() if isinstance(value, uuid.UUID)), None
            )
            return Rows([item for item in ids if after is None or item > after][:25])

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, Session())

    async def prepare(reservation_id: uuid.UUID, settings: Settings, now: datetime) -> None:
        visited.append(reservation_id)

    monkeypatch.setattr(image_residency, "session_scope", scope)
    monkeypatch.setattr(image_residency, "_prepare_idle_unload", prepare)
    monkeypatch.setattr(image_residency, "_scan_after", None)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert await image_residency.sweep_idle_image_workers(settings) == 0
    assert visited == ids[:25]
    assert await image_residency.sweep_idle_image_workers(settings) == 0
    assert visited[-1] == ids[25]
    assert await image_residency.sweep_idle_image_workers(settings) == 0
    assert visited[-25:] == ids[:25]
