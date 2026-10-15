"""Independent clocks and restart schedules for durable grant safety."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from coire_core.failover_safety import ElectionSafety, ElectionSafetyError
from coire_core.models.failover import ElectionVoteGrant


class Clock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.wall = datetime.now(UTC)

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds
        self.wall += timedelta(seconds=seconds)


def _safety(path: Path, clock: Clock, *, ttl: float = 15, epoch: int = 1) -> ElectionSafety:
    return ElectionSafety(
        path,
        member="coire-edge-b",
        epoch=epoch,
        lifetime_s=ttl,
        now=clock.wall,
        monotonic=clock.monotonic,
    )


def _grant(clock: Clock, holder: str, *, lifetime: float = 15, term: int = 1) -> ElectionVoteGrant:
    return ElectionVoteGrant(
        epoch=1,
        term=term,
        candidate=holder,
        voter="coire-edge-b",
        issued_at=clock.wall,
        expires_at=clock.wall + timedelta(seconds=lifetime),
        signature="fixture",
    )


def test_missing_state_restart_cannot_shorten_quarantine(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "safety.json"
    safety = _safety(path, clock)
    assert not safety.permits("coire-edge-a", clock.wall)
    clock.advance(300)
    assert not safety.permits("coire-edge-a", clock.wall)
    restarted = _safety(path, clock, ttl=1)
    clock.advance(1)
    assert not restarted.permits("coire-core", clock.wall)
    clock.advance(300)
    assert restarted.permits("coire-core", clock.wall)


def test_live_grant_survives_restart_lowered_ttl_and_new_epoch(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "safety.json"
    safety = _safety(path, clock, ttl=300)
    clock.advance(301)
    safety.grant(_grant(clock, "coire-edge-a", lifetime=300, term=7), clock.wall)
    restarted = _safety(path, clock, ttl=1, epoch=2)
    assert restarted.record.highest_term == 7
    assert restarted.record.prior_max_lifetime_s == 300
    assert not restarted.permits("coire-core", clock.wall)
    clock.advance(300)
    assert not restarted.permits("coire-core", clock.wall)
    clock.advance(1)
    assert restarted.permits("coire-core", clock.wall)


def test_self_grant_blocks_conflicting_term(tmp_path: Path) -> None:
    clock = Clock()
    safety = _safety(tmp_path / "safety.json", clock)
    clock.advance(301)
    safety.grant(_grant(clock, "coire-edge-b"), clock.wall)
    with pytest.raises(ElectionSafetyError):
        safety.grant(_grant(clock, "coire-core", term=100), clock.wall)


@pytest.mark.parametrize("jump", [-3600, 3600])
def test_wall_clock_jump_fences_without_shortening_durable_authority(
    tmp_path: Path, jump: int
) -> None:
    clock = Clock()
    path = tmp_path / "safety.json"
    safety = _safety(path, clock)
    clock.advance(301)
    safety.grant(_grant(clock, "coire-edge-a"), clock.wall)
    clock.wall += timedelta(seconds=jump)
    assert not safety.permits("coire-core", clock.wall)
    restarted = _safety(path, clock, ttl=1)
    clock.advance(300)
    assert not restarted.permits("coire-core", clock.wall)
    clock.advance(1)
    assert restarted.permits("coire-core", clock.wall)


def test_corrupt_state_quarantines_and_trusted_epoch_rollback_fences(tmp_path: Path) -> None:
    path = tmp_path / "safety.json"
    path.write_text("corrupt", encoding="utf-8")
    clock = Clock()
    safety = _safety(path, clock, epoch=3)
    assert not safety.permits("coire-core", clock.wall)
    clock.advance(301)
    assert safety.permits("coire-core", clock.wall)
    rollback = _safety(path, clock, epoch=2)
    clock.advance(1000)
    assert not rollback.permits("coire-core", clock.wall)


def test_snapshot_high_water_and_override_replay_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "safety.json"
    clock = Clock()
    safety = _safety(path, clock)
    assert safety.accept_snapshot(1, clock.wall)
    override = uuid4()
    assert safety.remember_override(override)
    restarted = _safety(path, clock)
    assert not restarted.accept_snapshot(1, clock.wall - timedelta(seconds=1))
    assert not restarted.remember_override(override)


def test_persistence_failure_cannot_issue_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    safety = _safety(tmp_path / "safety.json", clock)
    clock.advance(301)
    assert safety.permits("coire-core", clock.wall)

    def fail(*args: object) -> None:
        raise OSError("fixture persistence failure")

    monkeypatch.setattr("coire_core.failover_safety.os.replace", fail)
    with pytest.raises(ElectionSafetyError):
        safety.grant(_grant(clock, "coire-core"), clock.wall)
    assert not safety.permits("coire-core", clock.wall)
