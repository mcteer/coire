"""Recovery retains original identity, deadline and uncertain fixed stop dispatch."""

from datetime import timedelta
from pathlib import Path

import pytest
from test_handover_creation_journal import preparation

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_recovery_journal import HandoverRecoveryJournal


def recovery(tmp_path: Path, *, aged: bool = False) -> HandoverRecoveryJournal:
    value = preparation()
    if aged:
        value = value.model_copy(
            update={
                "prepared_at": value.prepared_at - timedelta(seconds=301),
                "deadline_at": value.deadline_at - timedelta(seconds=301),
            }
        )
    creation = HandoverCreationJournal(tmp_path, value)
    intent = creation.begin(now=value.prepared_at)
    creation.dispatch(intent, now=value.prepared_at)
    creation.observe(intent, "a" * 64, observed_at=value.prepared_at, now=value.prepared_at)
    creation.candidate_seed()
    return HandoverRecoveryJournal(creation)


def test_restart_and_lost_stop_response_never_renew_or_redispatch(tmp_path: Path) -> None:
    journal = recovery(tmp_path)
    at = journal.creation.preparation.deadline_at
    initial = journal.begin(now=at)
    assert initial.deadline_at == at + timedelta(seconds=300)
    assert journal.dispatch(now=at)
    restarted = HandoverRecoveryJournal(
        HandoverCreationJournal.open_retained(tmp_path, journal.creation.preparation)
    )
    retained = restarted.begin(now=initial.deadline_at + timedelta(seconds=60))
    assert retained.outcome == "dispatched"
    assert retained.deadline_at == initial.deadline_at
    assert not restarted.dispatch(now=initial.deadline_at + timedelta(seconds=60))
    observed = initial.deadline_at + timedelta(seconds=60)
    stopped = restarted.observe_stopped("a" * 64, observed_at=observed, now=observed)
    assert stopped.deadline_at == initial.deadline_at
    assert stopped.stopped_observed_at == observed
    assert (
        restarted.observe_stopped(
            "a" * 64,
            observed_at=observed + timedelta(seconds=60),
            now=observed + timedelta(seconds=60),
        )
        == stopped
    )


def test_expired_undispatched_intent_remains_fenced(tmp_path: Path) -> None:
    journal = recovery(tmp_path)
    initial = journal.begin(now=journal.creation.preparation.prepared_at)
    with pytest.raises(UpgradeUnavailable):
        journal.dispatch(now=initial.deadline_at)
    assert journal.current() == initial


@pytest.mark.parametrize("fault", ["incumbent", "foreign", "stale", "future", "predates"])
def test_stop_receipt_requires_exact_candidate_and_original_read(
    tmp_path: Path, fault: str
) -> None:
    journal = recovery(tmp_path)
    at = journal.creation.preparation.deadline_at
    initial = journal.begin(now=at)
    identity = "a" * 64
    observed, clock = at, at
    if fault == "incumbent":
        identity = initial.incumbent_container_id
    elif fault == "foreign":
        identity = "b" * 64
    elif fault == "stale":
        clock += timedelta(seconds=6)
    elif fault == "future":
        observed += timedelta(seconds=1)
    else:
        observed -= timedelta(seconds=1)
    with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
        journal.observe_stopped(identity, observed_at=observed, now=clock)
    assert journal.current() == initial


def test_already_stopped_positive_observation_needs_no_mutation(tmp_path: Path) -> None:
    journal = recovery(tmp_path)
    at = journal.creation.preparation.deadline_at
    initial = journal.begin(now=at)
    result = journal.observe_stopped("a" * 64, observed_at=at, now=at)
    assert result.outcome == "stopped"
    assert result.deadline_at == initial.deadline_at
    assert not journal.dispatch(now=at)
