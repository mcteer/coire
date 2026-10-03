"""The scheduler retries one fenced node cancellation before core finalization."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from coire_api.db import ImageJobRow, NodeRow
from coire_api.nodes_client import NodeError, NodeErrorKind
from coire_core.errors import ImageConflict
from coire_core.models.image_worker import NodeImageCancelRequest, NodeImageJob
from coire_core.settings import Settings
from coire_scheduler import images

JOB = "01J00000000000000000000000"
NODE_ID = uuid.uuid4()
INSTANCE = uuid.uuid4()


@pytest.mark.parametrize("wrong_fence", [False, True])
async def test_cancel_workflow_requires_exact_node_ack_before_finalization(
    monkeypatch: pytest.MonkeyPatch, wrong_fence: bool
) -> None:
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=JOB,
        state="cancelling",
        selected_node_id=NODE_ID,
        instance_id=INSTANCE,
        attempt=1,
        fence=3,
        cancel_requested_at=now,
    )
    calls: list[str] = []
    delay_samples: list[float] = []

    class Session:
        async def get(self, model: type[object], identity: object) -> Any:
            if model is ImageJobRow:
                assert identity == JOB
                return row
            assert model is NodeRow and identity == NODE_ID
            return SimpleNamespace(name="coire-edge-b")

    @asynccontextmanager
    async def scope() -> AsyncIterator[Session]:
        yield Session()

    class Client:
        def __init__(self, settings: Settings) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def cancel_image_job(
            self, node: str, command: NodeImageCancelRequest
        ) -> NodeImageJob:
            calls.append("node_cancel")
            assert node == "coire-edge-b"
            assert (command.job_id, command.attempt, command.fence) == (JOB, 1, 3)
            return NodeImageJob(
                job_id=JOB,
                attempt=1,
                fence=4 if wrong_fence else 3,
                node=node,
                instance_id=INSTANCE,
                state="cancelled",
                scratch_cleaned=True,
                updated_at=now,
            )

    async def finalize(
        session: object,
        status: NodeImageJob,
        selected_node_id: uuid.UUID,
        settings: Settings,
    ) -> bool:
        calls.append("core_finalize")
        assert selected_node_id == NODE_ID and status.scratch_cleaned
        return True

    monkeypatch.setattr(images, "session_scope", scope)
    monkeypatch.setattr(images, "NodeClient", Client)
    monkeypatch.setattr(images, "finalize_cancelled_image_job", finalize)
    monkeypatch.setattr(images, "get_settings", lambda: Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    monkeypatch.setattr(
        images,
        "cancellation_delay_seconds",
        SimpleNamespace(record=lambda value: delay_samples.append(value)),
    )
    if wrong_fence:
        with pytest.raises(ImageConflict):
            await images.drive_image_cancel(JOB)
        assert calls == ["node_cancel"]
        assert delay_samples == []
    else:
        await images.drive_image_cancel(JOB)
        assert calls == ["node_cancel", "core_finalize"]
        assert len(delay_samples) == 1 and delay_samples[0] >= 0


async def test_partition_keeps_committed_cancel_intent_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=JOB,
        state="cancelling",
        selected_node_id=NODE_ID,
        instance_id=INSTANCE,
        attempt=1,
        fence=3,
        cancel_requested_at=now,
    )
    calls: list[str] = []

    class Session:
        async def get(self, model: type[object], identity: object) -> Any:
            if model is ImageJobRow:
                return row
            assert model is NodeRow and identity == NODE_ID
            return SimpleNamespace(name="coire-edge-b")

    @asynccontextmanager
    async def scope() -> AsyncIterator[Session]:
        yield Session()

    class Client:
        def __init__(self, settings: Settings) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def cancel_image_job(
            self, node: str, command: NodeImageCancelRequest
        ) -> NodeImageJob:
            calls.append("node_cancel")
            raise NodeError(NodeErrorKind.UNREACHABLE, node)

    async def finalize(*args: object) -> bool:
        pytest.fail("unreachable node cannot prove cleanup")

    monkeypatch.setattr(images, "session_scope", scope)
    monkeypatch.setattr(images, "NodeClient", Client)
    monkeypatch.setattr(images, "finalize_cancelled_image_job", finalize)
    monkeypatch.setattr(images, "get_settings", lambda: Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    with pytest.raises(NodeError):
        await images.drive_image_cancel(JOB)
    assert calls == ["node_cancel"]
    assert row.state == "cancelling" and row.cancel_requested_at == now
