from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from coire_api.failover import reconcile
from coire_core.models.failover import (
    FailoverEvent,
    FailoverEventKind,
    FailoverOverride,
    FailoverOverrideKind,
)


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

    @asynccontextmanager
    async def begin_nested(self) -> AsyncIterator[None]:
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("with_override", [False, True])
async def test_reconcile_event_writes_one_audit_receipt(
    monkeypatch: pytest.MonkeyPatch, with_override: bool
) -> None:
    session = FakeSession()
    audit_id = uuid4()

    audited: list[dict[str, object]] = []

    async def fake_write_audit(*_: object, **fields: object) -> SimpleNamespace:
        audited.append(fields)
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
    if with_override:
        now = datetime.now(UTC)
        event = event.model_copy(
            update={
                "kind": FailoverEventKind.OVERRIDE_BREAK_GLASS,
                "override": FailoverOverride(
                    target_host=event.host,
                    epoch=1,
                    issued_at=now,
                    expires_at=now + timedelta(seconds=60),
                    kind=FailoverOverrideKind.BREAK_GLASS_PROMOTE,
                    actor_id=uuid4(),
                    reason="operator recovery",
                    fencing_evidence="independent origins fenced",
                    signature="isolated-signed-override",
                ),
            }
        )
    assert await reconcile.reconcile_event(session, event)  # type: ignore[arg-type]
    assert not await reconcile.reconcile_event(session, event)  # type: ignore[arg-type]
    assert len(session.added) == 1
    receipt = session.added[0]
    assert receipt.audit_id == audit_id  # type: ignore[attr-defined]

    detail = audited[0]["detail"]
    assert isinstance(detail, dict)
    if event.override:
        assert detail["signed_override"] == event.override.model_dump(mode="json")
    else:
        assert "signed_override" not in detail
