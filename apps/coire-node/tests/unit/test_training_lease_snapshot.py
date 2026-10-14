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
@pytest.mark.parametrize("jobs", [None, (), ("01M4PZBKVWEZZX5T638SRJR2S8",)])
async def test_upgrade_drain_requires_explicit_finished_training_lifecycle(
    tmp_path: Path, jobs: tuple[str, ...] | None
) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    clock = Clock()
    body = snapshot(clock, {}).model_dump(mode="json")
    if jobs is not None:
        body["unfinished_jobs"] = list(jobs)
    reader = TrainingLeaseSnapshotReader(
        agent.settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
        now=clock.now,
        monotonic=clock.monotonic,
    )
    try:
        inventory = await reader.upgrade_inventory()
        assert inventory.status == ("unknown" if jobs is None else "complete")
        assert tuple(entry.work_id for entry in inventory.entries) == (jobs or ())
        assert all(entry.state == "active" for entry in inventory.entries)
        clock.mono += 5
        # Replaying the same wall-clock observation cannot renew monotonic validity.
        assert (await reader.upgrade_inventory()).status == "unknown"
    finally:
        await reader.aclose()
        agent.close()


def test_training_lifecycle_snapshot_rejects_duplicate_ownership() -> None:
    body = snapshot(Clock(), {}).model_dump(mode="json")
    body["unfinished_jobs"] = ["01M4PZBKVWEZZX5T638SRJR2S8"] * 2
    with pytest.raises(ValueError, match="duplicate jobs"):
        NodeTrainingLeaseSnapshot.model_validate(body)


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
async def test_fetch_waits_for_same_future_observation_without_reissuing_or_renewing(
    tmp_path: Path,
) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    sampled = datetime.now(UTC) + timedelta(milliseconds=30)
    original = NodeTrainingLeaseSnapshot(
        node="coire-edge-a",
        sampled_at=sampled,
        expires_at=sampled + timedelta(seconds=5),
        active_leases={},
        unfinished_jobs=(),
    )
    calls: list[httpx.Request] = []

    def fetch(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=original.model_dump(mode="json"))

    reader = TrainingLeaseSnapshotReader(agent.settings, transport=httpx.MockTransport(fetch))
    try:
        await reader.refresh()
        assert datetime.now(UTC) >= sampled
        assert reader.snapshot == original
        assert len(calls) == 1
        assert reader(set()) == 0
        assert (
            reader.deadline - reader.monotonic()
            <= (original.expires_at - datetime.now(UTC)).total_seconds() + 0.01
        )
    finally:
        await reader.aclose()
        agent.close()


@pytest.mark.asyncio
async def test_early_timer_wakeup_rechecks_same_snapshot_before_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    clock = Clock()
    sampled = clock.wall + timedelta(milliseconds=3)
    original = NodeTrainingLeaseSnapshot(
        node="coire-edge-a",
        sampled_at=sampled,
        expires_at=sampled + timedelta(seconds=5),
        active_leases={},
        unfinished_jobs=(),
    )
    calls: list[httpx.Request] = []
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def early_sleep(delay: float) -> None:
        sleeps.append(delay)
        elapsed = delay / 2 if len(sleeps) == 1 else delay
        clock.wall += timedelta(seconds=elapsed)
        clock.mono += elapsed
        await real_sleep(0)

    def fetch(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=original.model_dump(mode="json"))

    monkeypatch.setattr("coire_node.training.lease_snapshot.asyncio.sleep", early_sleep)
    reader = TrainingLeaseSnapshotReader(
        agent.settings,
        transport=httpx.MockTransport(fetch),
        now=clock.now,
        monotonic=clock.monotonic,
    )
    try:
        await reader.refresh()
        assert len(calls) == 1 and len(sleeps) == 2
        assert sleeps == pytest.approx([0.003, 0.0015])
        assert clock.wall == sampled and reader.snapshot == original
        assert reader(set()) == 0
        assert reader.deadline == pytest.approx(clock.mono + 5)
    finally:
        await reader.aclose()
        agent.close()


@pytest.mark.asyncio
async def test_frozen_future_clock_refuses_within_original_fetch_budget(tmp_path: Path) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    clock = Clock()
    future = snapshot(clock, {}).model_copy(
        update={
            "sampled_at": clock.wall + timedelta(seconds=3),
            "expires_at": clock.wall + timedelta(seconds=8),
        }
    )
    reader = TrainingLeaseSnapshotReader(
        agent.settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=future.model_dump(mode="json"))
        ),
        now=clock.now,
    )
    try:
        started = reader.monotonic()
        with pytest.raises(TimeoutError):
            await reader.refresh()
        assert reader.monotonic() - started < 2.5
        assert reader.snapshot is None
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
