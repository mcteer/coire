"""Studio failover capacity is reserved in the authoritative ledger before an outage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import MemoryReservationRow, NodeMemoryLedgerRow
from coire_api.placement.service import ensure_ledgers
from coire_core.models.placement import MemoryReservationState, ReservationHolder


class _Nodes:
    def __init__(self, node: SimpleNamespace) -> None:
        self.node = node

    def scalars(self) -> _Nodes:
        return self

    def all(self) -> list[SimpleNamespace]:
        return [self.node]


class _Session:
    def __init__(self) -> None:
        self.node = SimpleNamespace(id=uuid4(), reachability="healthy")
        self.rows: list[object] = []

    async def execute(self, _query: object) -> _Nodes:
        return _Nodes(self.node)

    async def get(self, _model: object, _id: object) -> object | None:
        return None

    async def scalar(self, _query: object) -> object | None:
        return None

    def add(self, row: object) -> None:
        self.rows.append(row)

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_standing_failover_slice_is_held_before_election() -> None:
    session = _Session()
    await ensure_ledgers(
        cast(AsyncSession, session),
        budget_bytes=8 * 1024**3,
        sandbox_bytes=0,
        failover_bytes=256 * 1024**2,
    )
    assert any(isinstance(row, NodeMemoryLedgerRow) for row in session.rows)
    failover = next(
        row
        for row in session.rows
        if isinstance(row, MemoryReservationRow) and row.holder_id == "failover-frontend"
    )
    assert failover.bytes == 256 * 1024**2
    assert failover.holder_type is ReservationHolder.SANDBOX
    assert failover.state is MemoryReservationState.HELD
    assert failover.pinned
