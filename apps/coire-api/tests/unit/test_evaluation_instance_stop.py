"""An evaluation cancelled before launch cannot be resurrected by queued instance work."""

import inspect
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from coire_api.db import ModelInstanceRow, ModelVariantRow
from coire_core.models.instance import InstanceState


async def test_queued_launch_of_failed_instance_does_not_allocate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import coire_scheduler.instances as module

    instance = ModelInstanceRow(
        id=uuid.uuid4(),
        state=InstanceState.FAILED,
        policy="single:coire-edge-a",
        variant_id=uuid.uuid4(),
    )
    session = AsyncMock()

    async def lookup(row_type: object, *args: object, **kwargs: object) -> object:
        if row_type is ModelInstanceRow:
            return instance
        if row_type is ModelVariantRow:
            return ModelVariantRow(id=instance.variant_id, validated=True)
        raise AssertionError("terminal evaluation instance allocated new work")

    session.get.side_effect = lookup
    session.add = Mock(side_effect=AssertionError("terminal instance allocated new work"))

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncMock]:
        yield session

    async def no_fallback(identity: uuid.UUID) -> None:
        pass

    async def refuse_transition(*args: object, **kwargs: object) -> None:
        raise AssertionError("terminal evaluation instance was resurrected")

    monkeypatch.setattr(module, "session_scope", scope)
    monkeypatch.setattr(module, "_wait_for_fallback_teardown", no_fallback)
    monkeypatch.setattr(module, "transition", refuse_transition)
    await inspect.unwrap(module.execute_instance_launch)(str(instance.id))
    session.add.assert_not_called()
