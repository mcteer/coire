from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from coire_api.placement.executor import PlacementCommandExecutor
from coire_core.models.placement import OccupantReason
from coire_core.settings import Settings
from coire_scheduler.placement import (
    Candidate,
    CapacityRefused,
    NodeCapacity,
    idle_eligible,
    plan_admission,
)


def _candidate(
    *, age: int, bytes_: int = 40, pinned: bool = False, in_flight: int = 0
) -> Candidate:
    return Candidate(
        reservation_id=uuid.uuid4(),
        holder_id=str(uuid.uuid4()),
        bytes=bytes_,
        pinned=pinned,
        in_flight=in_flight,
        last_used_at=datetime.now(UTC) - timedelta(seconds=age),
    )


def test_exact_fit_needs_no_eviction() -> None:
    result = plan_admission(NodeCapacity(budget_bytes=100, reserved_bytes=60), 40, [])
    assert result.evictions == []


def test_lru_skips_busy_and_evicts_next_oldest_idle() -> None:
    oldest_busy = _candidate(age=30, in_flight=1)
    next_idle = _candidate(age=20)
    newest_idle = _candidate(age=10)
    result = plan_admission(
        NodeCapacity(budget_bytes=100, reserved_bytes=90),
        30,
        [newest_idle, oldest_busy, next_idle],
    )
    assert result.evictions == [next_idle.reservation_id]
    assert result.occupants[0].reason is OccupantReason.IN_USE


def test_pinned_only_capacity_refusal_names_occupants() -> None:
    pinned = _candidate(age=100, pinned=True)
    with pytest.raises(CapacityRefused) as caught:
        plan_admission(NodeCapacity(budget_bytes=100, reserved_bytes=90), 30, [pinned])
    assert caught.value.occupants[0].reason is OccupantReason.PINNED


def test_multiple_evictions_are_ordered_lru() -> None:
    oldest = _candidate(age=20, bytes_=20)
    newest = _candidate(age=10, bytes_=20)
    result = plan_admission(NodeCapacity(budget_bytes=100, reserved_bytes=90), 45, [newest, oldest])
    assert result.evictions == [oldest.reservation_id, newest.reservation_id]


def test_all_busy_capacity_refusal_is_actionable() -> None:
    busy = _candidate(age=100, in_flight=2)
    with pytest.raises(CapacityRefused) as caught:
        plan_admission(NodeCapacity(budget_bytes=100, reserved_bytes=90), 20, [busy])
    assert caught.value.occupants[0].holder_id == busy.holder_id
    assert caught.value.occupants[0].reason is OccupantReason.IN_USE


@pytest.mark.parametrize(
    ("pinned", "in_flight", "ttl", "age", "eligible"),
    [
        (False, 0, 60, 61, True),
        (True, 0, 60, 61, False),
        (False, 1, 60, 61, False),
        (False, 0, 60, 59, False),
        (False, 0, None, 1000, False),
    ],
)
def test_idle_ttl_respects_pin_lease_and_per_model_ttl(
    pinned: bool, in_flight: int, ttl: int | None, age: int, eligible: bool
) -> None:
    now = datetime.now(UTC)
    assert (
        idle_eligible(
            last_used_at=now - timedelta(seconds=age),
            ttl_seconds=ttl,
            pinned=pinned,
            in_flight=in_flight,
            now=now,
        )
        is eligible
    )


@pytest.mark.asyncio
async def test_unload_failure_is_persisted_instead_of_credited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = PlacementCommandExecutor(Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    command_id = uuid.uuid4()
    failures: list[tuple[uuid.UUID, Exception]] = []

    async def fail(_command_id: uuid.UUID) -> dict[str, object]:
        raise RuntimeError("unload did not confirm")

    async def record(failed_id: uuid.UUID, exc: Exception) -> None:
        failures.append((failed_id, exc))

    monkeypatch.setattr(executor, "_execute", fail)
    monkeypatch.setattr(executor, "_failed", record)
    await executor._execute_safely(command_id)
    assert failures and failures[0][0] == command_id
    assert "did not confirm" in str(failures[0][1])
