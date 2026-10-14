"""Creation intent survives a lost Docker response without recapturing its deadline."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from upgrade_bootstrap_fixtures import prepared_bootstrap
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import CoreHandoverPreparation
from coire_upgrade_executor.handover_creation import HandoverCreationJournal


def preparation() -> CoreHandoverPreparation:
    seed, _ = prepared_bootstrap()
    now = datetime.now(UTC)
    candidate = core_plan().target
    return CoreHandoverPreparation(
        incumbent=seed,
        candidate_release=candidate,
        candidate_runtime_id="candidate-executor",
        candidate_image_id="sha256:" + "f" * 64,
        generation=seed.generation + 1,
        initiator_user_id=candidate.prepared_by,
        idempotency_key=uuid4(),
        prepared_at=now,
        deadline_at=now + timedelta(seconds=120),
    )


def test_restart_retains_creation_intent_and_original_deadline(tmp_path: Path) -> None:
    request = preparation()
    journal = HandoverCreationJournal(tmp_path, request)
    initial = journal.begin(now=request.prepared_at)
    assert initial.candidate_container_id is None
    restarted = HandoverCreationJournal(tmp_path, request)
    assert restarted.begin(now=request.deadline_at + timedelta(seconds=1)) == initial
    for update in ({"candidate_runtime_id": "other"}, {"initiator_user_id": uuid4()}):
        with pytest.raises(UpgradeConflict):
            HandoverCreationJournal(tmp_path, request.model_copy(update=update))
    # A late create response still records reality for cleanup, but grants no start/ownership.
    finished = restarted.observe(
        initial,
        "a" * 64,
        observed_at=request.deadline_at + timedelta(seconds=1),
        now=request.deadline_at + timedelta(seconds=1),
    )
    assert finished.candidate_container_id == "a" * 64
    assert (
        restarted.observe(
            initial,
            "a" * 64,
            observed_at=request.deadline_at + timedelta(seconds=2),
            now=request.deadline_at + timedelta(seconds=2),
        )
        == finished
    )
    assert request.deadline_at == restarted.preparation.deadline_at
    with pytest.raises(UpgradeConflict):
        restarted.observe(
            initial,
            "b" * 64,
            observed_at=request.deadline_at + timedelta(seconds=2),
            now=request.deadline_at + timedelta(seconds=2),
        )


def test_new_expired_creation_cannot_publish_docker_intent(tmp_path: Path) -> None:
    request = preparation()
    journal = HandoverCreationJournal(tmp_path, request)
    with pytest.raises(UpgradeUnavailable):
        journal.begin(now=request.deadline_at)
    assert journal.current() is None


def test_dispatch_is_written_once_before_a_lost_create_response(tmp_path: Path) -> None:
    request = preparation()
    journal = HandoverCreationJournal(tmp_path, request)
    effect = journal.begin(now=request.prepared_at)
    assert journal.dispatch(effect, now=request.prepared_at)
    restarted = HandoverCreationJournal(tmp_path, request)
    retained = restarted.current()
    assert retained is not None and retained.outcome == "dispatched"
    assert not restarted.dispatch(effect, now=request.prepared_at + timedelta(seconds=1))
    observed = restarted.observe(
        effect,
        "a" * 64,
        observed_at=request.prepared_at + timedelta(seconds=1),
        now=request.prepared_at + timedelta(seconds=1),
    )
    assert not restarted.dispatch(effect, now=request.prepared_at + timedelta(seconds=2))
    assert restarted.current() == observed


@pytest.mark.parametrize(
    "field,value",
    [
        ("candidate_runtime_id", "initial-executor"),
        ("candidate_image_id", "sha256:" + "e" * 64),
        ("generation", 1),
        ("initiator_user_id", "not-a-uuid"),
        ("protocol", 2),
        ("deadline_at", datetime.now(UTC) + timedelta(seconds=300)),
        ("command", "exec"),
    ],
)
def test_preparation_refuses_changed_authority_and_deadline(field: str, value: object) -> None:
    from pydantic import ValidationError

    request = preparation()
    with pytest.raises(ValidationError):
        CoreHandoverPreparation.model_validate(request.model_dump() | {field: value})


def test_deleted_preparation_cannot_become_a_new_create_intent(tmp_path: Path) -> None:
    request = preparation()
    journal = HandoverCreationJournal(tmp_path, request)
    journal.begin(now=request.prepared_at)
    journal.store.path("preparation.json").unlink()
    with pytest.raises(UpgradeConflict):
        journal.begin(now=request.prepared_at)
    with pytest.raises(UpgradeConflict):
        HandoverCreationJournal(tmp_path, request)


def test_candidate_seed_keeps_application_source_and_creation_observation(tmp_path: Path) -> None:
    request = preparation()
    journal = HandoverCreationJournal(tmp_path, request)
    effect = journal.begin(now=request.prepared_at)
    with pytest.raises(UpgradeConflict):
        journal.candidate_seed()
    observed_at = request.prepared_at + timedelta(seconds=1)
    journal.observe(effect, "a" * 64, observed_at=observed_at, now=observed_at)
    seed = journal.candidate_seed()
    assert seed.source == request.incumbent.source
    assert seed.source_container_ids == request.incumbent.source_container_ids
    assert seed.executor_source == request.candidate_release
    assert seed.executor_container_id == "a" * 64
    assert seed.prepared_at == observed_at
    assert HandoverCreationJournal(tmp_path, request).candidate_seed() == seed


def test_passive_reader_cannot_publish_missing_preparation_or_candidate_seed(
    tmp_path: Path,
) -> None:
    request = preparation()
    with pytest.raises(UpgradeUnavailable):
        HandoverCreationJournal.open_retained(tmp_path / "absent", request)
    assert not (tmp_path / "absent").exists()
    journal = HandoverCreationJournal(tmp_path / "retained", request)
    effect = journal.begin(now=request.prepared_at)
    journal.observe(effect, "a" * 64, observed_at=request.prepared_at, now=request.prepared_at)
    reader = HandoverCreationJournal.open_retained(journal.store.root, request)
    with pytest.raises(UpgradeUnavailable):
        reader.retained_candidate_seed()
    assert not journal.store.path("candidate-seed.json").exists()
    seed = journal.candidate_seed()
    assert reader.retained_candidate_seed() == seed
    journal.store.write(
        "candidate-seed.json", seed.model_copy(update={"executor_runtime_id": "other"})
    )
    with pytest.raises(UpgradeConflict):
        reader.retained_candidate_seed()


def test_candidate_start_intent_cannot_renew_dispatch_or_process_birth(tmp_path: Path) -> None:
    request = preparation()
    journal = HandoverCreationJournal(tmp_path, request)
    creation = journal.begin(now=request.prepared_at)
    journal.observe(creation, "a" * 64, observed_at=request.prepared_at, now=request.prepared_at)
    seed = journal.candidate_seed()
    intent = journal.begin_start(now=request.prepared_at)
    assert intent.candidate_seed_sha256 == seed.sha256()
    assert intent.deadline_at == request.deadline_at
    assert journal.dispatch_start(intent, now=request.prepared_at)
    restarted = HandoverCreationJournal.open_retained(tmp_path, request)
    assert not restarted.dispatch_start(intent, now=request.prepared_at)
    assert (
        restarted.begin_start(now=request.deadline_at + timedelta(seconds=1)).deadline_at
        == request.deadline_at
    )
    started_at = request.prepared_at + timedelta(seconds=1)
    observed_at = request.prepared_at + timedelta(seconds=2)
    observed = restarted.observe_start(
        started_at=started_at, observed_at=observed_at, now=observed_at
    )
    assert observed.outcome == "observed"
    assert (
        restarted.observe_start(
            started_at=started_at,
            observed_at=observed_at + timedelta(seconds=1),
            now=observed_at + timedelta(seconds=1),
        )
        == observed
    )
    with pytest.raises(UpgradeConflict):
        restarted.observe_start(
            started_at=started_at + timedelta(seconds=1), observed_at=observed_at, now=observed_at
        )


def test_candidate_start_cannot_dispatch_after_original_deadline(tmp_path: Path) -> None:
    request = preparation()
    journal = HandoverCreationJournal(tmp_path, request)
    with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
        journal.begin_start(now=request.prepared_at)
    creation = journal.begin(now=request.prepared_at)
    journal.observe(creation, "a" * 64, observed_at=request.prepared_at, now=request.prepared_at)
    journal.candidate_seed()
    intent = journal.begin_start(now=request.prepared_at)
    with pytest.raises(UpgradeUnavailable):
        journal.dispatch_start(intent, now=request.deadline_at)
    assert journal.current_start() == intent


def test_passive_proof_cannot_change_the_observed_candidate_or_original_deadline(
    tmp_path: Path,
) -> None:
    from coire_core.models.upgrades import ExecutorPassiveReadiness

    request = preparation()
    journal = HandoverCreationJournal(tmp_path, request)
    created = journal.begin(now=request.prepared_at)
    journal.observe(created, "a" * 64, observed_at=request.prepared_at, now=request.prepared_at)
    seed = journal.candidate_seed()
    intent = journal.begin_start(now=request.prepared_at)
    journal.dispatch_start(intent, now=request.prepared_at)
    journal.observe_start(
        started_at=request.prepared_at, observed_at=request.prepared_at, now=request.prepared_at
    )
    proof = ExecutorPassiveReadiness(
        preparation_sha256=request.sha256(),
        candidate_seed_sha256=seed.sha256(),
        generation=request.generation,
        candidate_runtime_id=seed.executor_runtime_id,
        candidate_container_id=seed.executor_container_id,
        candidate_image_id=seed.executor_image_id,
        incumbent_runtime_id=request.incumbent.executor_runtime_id,
        incumbent_container_id=request.incumbent.executor_container_id,
        observed_at=request.prepared_at,
    )
    journal.record_passive(proof, now=request.prepared_at)
    assert journal.store.read("passive-proof.json", ExecutorPassiveReadiness) == proof
    with pytest.raises(UpgradeConflict):
        journal.record_passive(
            proof.model_copy(update={"candidate_seed_sha256": "f" * 64}), now=request.prepared_at
        )
    with pytest.raises(UpgradeUnavailable):
        journal.record_passive(proof, now=request.deadline_at)
    assert journal.preparation.deadline_at == request.deadline_at


def test_consumed_transfer_freezes_protocol_evidence_and_cannot_renew_deadline(
    tmp_path: Path,
) -> None:
    from coire_core.models.upgrades import CoreHandoverTransferIntent, ExecutorPassiveReadiness

    request = preparation()
    journal = HandoverCreationJournal(tmp_path, request)
    created = journal.begin(now=request.prepared_at)
    journal.observe(created, "a" * 64, observed_at=request.prepared_at, now=request.prepared_at)
    seed = journal.candidate_seed()
    start = journal.begin_start(now=request.prepared_at)
    journal.dispatch_start(start, now=request.prepared_at)
    journal.observe_start(
        started_at=request.prepared_at, observed_at=request.prepared_at, now=request.prepared_at
    )
    proof = ExecutorPassiveReadiness(
        preparation_sha256=request.sha256(),
        candidate_seed_sha256=seed.sha256(),
        generation=request.generation,
        candidate_runtime_id=seed.executor_runtime_id,
        candidate_container_id=seed.executor_container_id,
        candidate_image_id=seed.executor_image_id,
        incumbent_runtime_id=request.incumbent.executor_runtime_id,
        incumbent_container_id=request.incumbent.executor_container_id,
        observed_at=request.prepared_at,
    )
    journal.record_passive(proof, now=request.prepared_at)
    intent = CoreHandoverTransferIntent(
        handover_id=uuid4(),
        intent_id=uuid4(),
        preparation_sha256=request.sha256(),
        candidate_seed_sha256=seed.sha256(),
        passive_proof_sha256=proof.sha256(),
        generation=request.generation,
        consumed_at=request.prepared_at,
        deadline_at=request.prepared_at + timedelta(seconds=30),
    )
    journal.record_transfer(intent)
    restarted = HandoverCreationJournal.open_retained(tmp_path, request)
    assert restarted.retained_transfer() == intent
    restarted.record_transfer(intent)
    with pytest.raises(UpgradeConflict):
        restarted.record_transfer(intent.model_copy(update={"intent_id": uuid4()}))
    with pytest.raises(UpgradeConflict):
        restarted.record_passive(
            proof.model_copy(update={"observed_at": request.prepared_at + timedelta(seconds=1)}),
            now=request.prepared_at + timedelta(seconds=1),
        )
    assert restarted.retained_transfer().deadline_at == intent.deadline_at
