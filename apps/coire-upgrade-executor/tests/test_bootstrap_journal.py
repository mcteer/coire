"""A restart cannot bypass the incumbent or renew a claimed stability interval."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import BootstrapHandover, BootstrapStabilityObservation
from coire_upgrade_executor.bootstrap import BootstrapJournal


def handover(now: datetime) -> BootstrapHandover:
    return BootstrapHandover(
        incumbent_id="executor-source",
        candidate_id="executor-target",
        incumbent_container_id="c" * 64,
        candidate_container_id="d" * 64,
        incumbent_image_id="sha256:" + "a" * 64,
        candidate_image_id="sha256:" + "b" * 64,
        generation=1,
        phase="passive",
        deadline_at=now + timedelta(seconds=120),
        updated_at=now,
    )


def observation(value: BootstrapHandover, at: datetime) -> BootstrapStabilityObservation:
    assert value.stability_started_at is not None
    return BootstrapStabilityObservation(
        generation=value.generation,
        incumbent_id=value.incumbent_id,
        candidate_id=value.candidate_id,
        incumbent_container_id="c" * 64,
        candidate_container_id="d" * 64,
        incumbent_image_id=value.incumbent_image_id,
        candidate_image_id=value.candidate_image_id,
        kernel_boot_identity="isolated-kernel",
        candidate_started_at=value.stability_started_at - timedelta(seconds=1),
        observed_at=at,
        incumbent_healthy=True,
        candidate_healthy=True,
        exclusive_writer_verified=True,
    )


def test_restart_preserves_writer_handover_and_original_stability_window(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    journal = BootstrapJournal(tmp_path / "journal")
    initial = journal.begin(handover(now), now=now)
    with pytest.raises(UpgradeConflict):
        journal.advance(initial, "claimed", now=now)
    verified = journal.advance(initial, "verified", now=now + timedelta(seconds=1))
    claimed = journal.advance(verified, "claimed", now=now + timedelta(seconds=2))
    restarted = BootstrapJournal(tmp_path / "journal")
    assert restarted.begin(handover(now), now=now) == claimed
    assert restarted.advance(claimed, "claimed", now=now + timedelta(seconds=100)) == claimed
    with pytest.raises(UpgradeConflict):
        restarted.advance(claimed, "stable", now=claimed.deadline_at - timedelta(microseconds=1))
    assert claimed.stability_started_at is not None
    for elapsed in range(0, 121, 10):
        at = claimed.stability_started_at + timedelta(seconds=elapsed)
        assert restarted.observe_stability(observation(claimed, at), now=at) == claimed
    stable = restarted.advance(claimed, "stable", now=claimed.deadline_at)
    committed = restarted.advance(stable, "committed", now=claimed.deadline_at)
    assert committed.stability_started_at == claimed.stability_started_at
    assert BootstrapJournal(tmp_path / "journal").current() == committed


def test_stale_stable_candidate_cannot_commit_without_retained_incumbent(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    journal = BootstrapJournal(tmp_path / "journal")
    initial = journal.begin(handover(now), now=now)
    verified = journal.advance(initial, "verified", now=now)
    claimed = journal.advance(verified, "claimed", now=now)
    for elapsed in range(0, 121, 10):
        at = now + timedelta(seconds=elapsed)
        journal.observe_stability(observation(claimed, at), now=at)
    stable = journal.advance(claimed, "stable", now=claimed.deadline_at)
    with pytest.raises(UpgradeUnavailable):
        journal.advance(stable, "committed", now=claimed.deadline_at + timedelta(seconds=11))
    assert journal.current() == stable
    assert stable.incumbent_id == initial.incumbent_id


def test_expired_candidate_retains_incumbent_and_blocks_another_handover(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    journal = BootstrapJournal(tmp_path / "journal")
    initial = journal.begin(handover(now), now=now)
    with pytest.raises(UpgradeUnavailable):
        journal.advance(initial, "verified", now=initial.deadline_at)
    restoring = journal.advance(initial, "restoring", now=initial.deadline_at)
    assert restoring.incumbent_image_id == initial.incumbent_image_id
    assert journal.current() == restoring
    different = handover(now).model_copy(update={"candidate_id": "executor-other", "generation": 2})
    with pytest.raises(UpgradeConflict):
        journal.begin(different, now=now)
    with pytest.raises(UpgradeConflict):
        journal.advance(restoring, "committed", now=initial.deadline_at)
    restored = journal.advance(restoring, "restored", now=initial.deadline_at)
    later = initial.deadline_at + timedelta(seconds=1)
    next_intent = handover(later).model_copy(update={"generation": 2})
    assert journal.begin(next_intent, now=later) == next_intent
    assert restored.incumbent_image_id == next_intent.incumbent_image_id


def test_stale_supervisor_cannot_advance_newer_state(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    journal = BootstrapJournal(tmp_path / "journal")
    initial = journal.begin(handover(now), now=now)
    journal.advance(initial, "verified", now=now)
    with pytest.raises(UpgradeConflict):
        journal.advance(initial, "restoring", now=now)


def test_handover_wire_refuses_self_supervision_or_missing_stability_window() -> None:
    data = handover(datetime.now(UTC)).model_dump()
    with pytest.raises(ValidationError):
        BootstrapHandover.model_validate(data | {"candidate_id": data["incumbent_id"]})
    with pytest.raises(ValidationError):
        BootstrapHandover.model_validate(data | {"phase": "committed"})
    with pytest.raises(ValidationError):
        BootstrapHandover.model_validate(
            data | {"candidate_container_id": data["incumbent_container_id"]}
        )


def test_first_observation_cannot_replace_the_prepared_candidate(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    journal = BootstrapJournal(tmp_path / "journal")
    initial = journal.begin(handover(now), now=now)
    verified = journal.advance(initial, "verified", now=now)
    claimed = journal.advance(verified, "claimed", now=now)
    replacement = observation(claimed, now).model_copy(update={"candidate_container_id": "e" * 64})
    restored = journal.observe_stability(replacement, now=now)
    assert restored.phase == "restoring"
    assert restored.deadline_at == claimed.deadline_at
    assert not journal.store.path("stability-observation.json").exists()


@pytest.mark.parametrize("fault", ["gap", "reboot", "replacement", "restart"])
def test_stability_interruption_restores_without_renewing_window(
    tmp_path: Path, fault: str
) -> None:
    now = datetime.now(UTC)
    journal = BootstrapJournal(tmp_path / "journal")
    initial = journal.begin(handover(now), now=now)
    verified = journal.advance(initial, "verified", now=now)
    claimed = journal.advance(verified, "claimed", now=now)
    first = observation(claimed, now)
    journal.observe_stability(first, now=now)
    later = now + timedelta(seconds=11 if fault == "gap" else 5)
    updates: dict[str, object] = {"observed_at": later}
    if fault == "reboot":
        updates["kernel_boot_identity"] = "another-kernel"
    elif fault == "replacement":
        updates["candidate_container_id"] = "e" * 64
    elif fault == "restart":
        updates["candidate_started_at"] = later - timedelta(seconds=1)
    changed = BootstrapStabilityObservation.model_validate(first.model_dump() | updates)
    restarted = BootstrapJournal(tmp_path / "journal")
    restoring = restarted.observe_stability(changed, now=later)
    assert restoring.phase == "restoring"
    assert restoring.deadline_at == claimed.deadline_at
    assert restoring.stability_started_at == claimed.stability_started_at
    assert (
        restarted.store.read("stability-observation.json", BootstrapStabilityObservation) == first
    )
    with pytest.raises(UpgradeConflict):
        restarted.advance(restoring, "stable", now=claimed.deadline_at)


def test_elapsed_wall_clock_alone_cannot_prove_candidate_stability(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    journal = BootstrapJournal(tmp_path / "journal")
    initial = journal.begin(handover(now), now=now)
    verified = journal.advance(initial, "verified", now=now)
    claimed = journal.advance(verified, "claimed", now=now)
    with pytest.raises(UpgradeUnavailable):
        journal.advance(claimed, "stable", now=claimed.deadline_at)
    assert journal.current() == claimed
    restoring = journal.observe_stability(
        observation(claimed, claimed.deadline_at), now=claimed.deadline_at
    )
    assert restoring.phase == "restoring"
