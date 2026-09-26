from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from coire_api.failover import reconcile
from coire_core.models.failover import FailoverEvent, FailoverEventKind


class FakeSession:
    def __init__(self) -> None:
        self.receipts: dict[object, object] = {}
        self.added: list[object] = []

    async def get(self, model: type[object], event_id: object) -> object | None:
        return self.receipts.get(event_id)

    def add(self, row: object) -> None:
        self.added.append(row)
        self.receipts[cast(object, row.event_id)] = row  # type: ignore[attr-defined]

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_reconcile_event_writes_one_audit_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    audit_id = uuid4()

    async def fake_write_audit(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(id=audit_id)

    monkeypatch.setattr(reconcile, "write_audit", fake_write_audit)
    event = FailoverEvent(
        event_id=uuid4(),
        term=3,
        kind=FailoverEventKind.PROMOTED,
        host="coire-edge-a",
        occurred_at=datetime.now(UTC),
        proof_digest="d" * 64,
    )
    assert await reconcile.reconcile_event(session, event)  # type: ignore[arg-type]
    assert not await reconcile.reconcile_event(session, event)  # type: ignore[arg-type]
    assert len(session.added) == 1
    receipt = session.added[0]
    assert receipt.audit_id == audit_id  # type: ignore[attr-defined]
