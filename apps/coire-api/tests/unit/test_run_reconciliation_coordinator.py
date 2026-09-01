from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Self, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.run_reconciler import RunReconciliationCoordinator
from coire_core.models.runs import RunReconcileRequest, RunReconcileResult
from coire_core.settings import Settings


async def test_orphan_reap_refreshes_authority_after_placement_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_id, run_id = uuid.uuid4(), uuid.uuid4()
    node = SimpleNamespace(id=node_id, name="coire-edge-a")
    unplaced = SimpleNamespace(id=run_id, node_id=None)
    sessions: Iterator[list[Any]] = iter([[node], [unplaced], [run_id]])

    class ScalarRows:
        def __init__(self, rows: list[Any]) -> None:
            self.rows = rows

        def all(self) -> list[Any]:
            return self.rows

    class Session:
        async def scalars(self, _: object) -> ScalarRows:
            return ScalarRows(next(sessions))

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, Session())

    requests: list[RunReconcileRequest] = []

    class Client:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def reconcile_runs(self, _: str, request: RunReconcileRequest) -> RunReconcileResult:
            requests.append(request)
            return RunReconcileResult(
                observations=[],
                orphan_run_ids=[run_id] if len(requests) == 1 else [],
                reaped_run_ids=[],
            )

    monkeypatch.setattr("coire_api.run_reconciler.session_scope", scope)
    monkeypatch.setattr("coire_api.run_reconciler.NodeClient", Client)
    coordinator = RunReconciliationCoordinator(
        Settings(_secrets_dir="/none")  # type: ignore[call-arg]
    )
    await coordinator.reconcile_once()

    assert requests == [
        RunReconcileRequest(authoritative_run_ids=frozenset(), reap_orphans=False),
        RunReconcileRequest(authoritative_run_ids=frozenset({run_id}), reap_orphans=True),
    ]
