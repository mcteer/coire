"""Authenticated bounded lease observations and monotonic fail-closed admission cache."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from coire_core.errors import TrainingConflict
from coire_core.models.training_node import NodeTrainingLeaseSnapshot
from coire_node.testing.harness import TOKEN, Agent
from coire_node.training.lease_snapshot import TrainingLeaseSnapshotReader


class Clock:
    def __init__(self) -> None:
        self.wall = datetime.now(UTC)
        self.mono = 100.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


def snapshot(clock: Clock, leases: dict[uuid.UUID, int]) -> NodeTrainingLeaseSnapshot:
    return NodeTrainingLeaseSnapshot(
        node="coire-edge-a",
        sampled_at=clock.wall,
        expires_at=clock.wall + timedelta(seconds=5),
        active_leases=leases,
    )


@pytest.mark.asyncio
async def test_authenticated_fetch_exact_scope_and_missing_instances_refuse(tmp_path: Path) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    clock = Clock()
    first, second = uuid.uuid4(), uuid.uuid4()
    calls: list[httpx.Request] = []

    def fetch(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.headers["X-Coire-Node"] == "coire-edge-a"
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.url.path == "/api/v1/internal/training/nodes/coire-edge-a/leases"
        return httpx.Response(
            200, json=snapshot(clock, {first: 0, second: 2}).model_dump(mode="json")
        )

    reader = TrainingLeaseSnapshotReader(
        agent.settings,
        transport=httpx.MockTransport(fetch),
        now=clock.now,
        monotonic=clock.monotonic,
    )
    try:
        with pytest.raises(TrainingConflict):
            reader(set())
        await reader.refresh()
        assert reader({first}) == 0 and reader({second}) == 2 and reader(set()) == 2
        with pytest.raises(TrainingConflict, match="omits"):
            reader({first, uuid.uuid4()})
        assert len(calls) == 1  # Cache callbacks never issue HTTP under an admission lock.
        clock.mono += 5
        with pytest.raises(TrainingConflict, match="stale"):
            reader({first})
    finally:
        await reader.aclose()
        agent.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["node", "expired", "old", "future", "long_ttl"])
async def test_snapshot_scope_and_time_bounds(tmp_path: Path, case: str) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    clock = Clock()
    reader = TrainingLeaseSnapshotReader(agent.settings, now=clock.now, monotonic=clock.monotonic)
    valid = snapshot(clock, {})
    changes: dict[str, dict[str, Any]] = {
        "node": {"node": "coire-edge-b"},
        "expired": {
            "sampled_at": clock.wall - timedelta(seconds=2),
            "expires_at": clock.wall - timedelta(seconds=1),
        },
        "old": {"sampled_at": clock.wall - timedelta(seconds=6)},
        "future": {"sampled_at": clock.wall + timedelta(seconds=1)},
        "long_ttl": {"expires_at": clock.wall + timedelta(seconds=6)},
    }
    try:
        with pytest.raises(TrainingConflict):
            reader.accept(valid.model_copy(update=changes[case]))
        with pytest.raises(TrainingConflict):
            reader(set())
    finally:
        await reader.aclose()
        agent.close()


@pytest.mark.asyncio
async def test_replay_cannot_extend_monotonic_authority_or_rewrite_counts(tmp_path: Path) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    clock = Clock()
    identity = uuid.uuid4()
    reader = TrainingLeaseSnapshotReader(agent.settings, now=clock.now, monotonic=clock.monotonic)
    original = snapshot(clock, {identity: 0})
    try:
        reader.accept(original)
        clock.mono += 4
        reader.accept(
            original
        )  # Frozen wall time cannot turn replay into nine seconds of authority.
        with pytest.raises(TrainingConflict, match="changed"):
            reader.accept(original.model_copy(update={"active_leases": {identity: 1}}))
        with pytest.raises(TrainingConflict, match="rewound"):
            reader.accept(
                original.model_copy(
                    update={
                        "sampled_at": clock.wall - timedelta(seconds=1),
                        "expires_at": clock.wall + timedelta(seconds=4),
                    }
                )
            )
        clock.mono += 1
        with pytest.raises(TrainingConflict):
            reader({identity})
        clock.wall -= timedelta(seconds=1)
        with pytest.raises(TrainingConflict):
            reader({identity})
    finally:
        await reader.aclose()
        agent.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "http://other.lab:8180",
        "http://coire-core.lab:8180/wrong",
        "http://name:secret@coire-core.lab:8180",
        "http://coire-core.lab:8180?token=x",
    ],
)
async def test_wrong_origin_never_receives_node_credential(tmp_path: Path, origin: str) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    requests: list[httpx.Request] = []

    def fetch(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    reader = TrainingLeaseSnapshotReader(agent.settings, transport=httpx.MockTransport(fetch))
    try:
        reader.accept(
            NodeTrainingLeaseSnapshot(
                node="coire-edge-a",
                sampled_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(seconds=4),
                active_leases={},
            )
        )
        agent.settings.training_input_api_url = origin
        with pytest.raises(TrainingConflict):
            await reader.refresh()
        with pytest.raises(TrainingConflict):
            reader(set())
        assert not requests
    finally:
        await reader.aclose()
        agent.close()


@pytest.mark.asyncio
async def test_poll_auth_failure_keeps_only_unexpired_valid_snapshot_and_closes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    clock = Clock()
    second = asyncio.Event()
    calls = 0

    def fetch(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=snapshot(clock, {}).model_dump(mode="json"))
        second.set()
        return httpx.Response(401)

    reader = TrainingLeaseSnapshotReader(
        agent.settings,
        transport=httpx.MockTransport(fetch),
        now=clock.now,
        monotonic=clock.monotonic,
    )
    try:
        await asyncio.gather(reader.start(), reader.start())
        task = reader.task
        await reader.start()
        assert reader.task is task and calls == 1 and reader(set()) == 0
        await asyncio.wait_for(second.wait(), timeout=2)
        assert reader(set()) == 0
        clock.mono += 5
        with pytest.raises(TrainingConflict):
            reader(set())
        assert TOKEN not in caplog.text
    finally:
        await reader.aclose()
        assert reader.client.is_closed and reader.task is not None and reader.task.done()
        with pytest.raises(TrainingConflict):
            reader(set())
        agent.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_kind", ["redirect", "wrong_path", "wrong_node", "oversized", "string_count"]
)
async def test_untrusted_response_cannot_publish_a_cache(
    tmp_path: Path, response_kind: str
) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    clock = Clock()

    def fetch(request: httpx.Request) -> httpx.Response:
        body = snapshot(clock, {}).model_dump(mode="json")
        if response_kind == "redirect":
            return httpx.Response(302, headers={"Location": "http://other.lab/private"})
        if response_kind == "wrong_path":
            request.url = httpx.URL("http://coire-core.lab:8180/wrong")
            return httpx.Response(200, json=body)
        if response_kind == "oversized":
            return httpx.Response(200, content=b"x" * (64 * 1024 + 1))
        if response_kind == "wrong_node":
            body["node"] = "coire-edge-b"
        if response_kind == "string_count":
            body["active_leases"] = {str(uuid.uuid4()): "0"}
        return httpx.Response(200, json=body)

    reader = TrainingLeaseSnapshotReader(
        agent.settings,
        transport=httpx.MockTransport(fetch),
        now=clock.now,
        monotonic=clock.monotonic,
    )
    try:
        await reader.observe()
        with pytest.raises(TrainingConflict):
            reader(set())
    finally:
        await reader.aclose()
        agent.close()
