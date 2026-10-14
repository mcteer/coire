"""Candidate claims require consumed intent, original deadlines and an independent incumbent."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_handover_creation_journal import preparation
from upgrade_bootstrap_fixtures import prepared_bootstrap

from coire_api.upgrades.handover_host_hold import publish_handover_hold
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreHandoverIncumbentObservation,
    CoreHandoverTransferIntent,
    ExecutorPassiveReadiness,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_ownership import candidate_handover_ownership
from coire_upgrade_executor.ownership import bootstrap_ownership


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "held_incumbent",
        "expired",
        "missing_intent",
        "wrong_incumbent",
        "restarted_candidate",
        "phase",
    ],
)
def test_candidate_cannot_become_its_own_rollback_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    import coire_upgrade_executor.handover_ownership as module

    monkeypatch.setattr(module, "verify_executor_namespace", lambda seed: None)
    value = preparation()
    _, source_physical = prepared_bootstrap(value.incumbent.source)
    source_physical = source_physical.model_copy(update={"seed_sha256": value.incumbent.sha256()})
    settings = Settings(
        upgrades_enabled=True,
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    root = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    root.write("handover-preparation.json", value)
    journal = HandoverCreationJournal(root.root / "handover" / str(value.generation), value)
    creation = journal.begin(now=value.prepared_at)
    journal.observe(creation, "a" * 64, observed_at=value.prepared_at, now=value.prepared_at)
    seed = journal.candidate_seed()
    start = journal.begin_start(now=value.prepared_at)
    journal.dispatch_start(start, now=value.prepared_at)
    journal.observe_start(
        started_at=value.prepared_at, observed_at=value.prepared_at, now=value.prepared_at
    )
    proof = ExecutorPassiveReadiness(
        preparation_sha256=value.sha256(),
        candidate_seed_sha256=seed.sha256(),
        generation=value.generation,
        candidate_runtime_id=seed.executor_runtime_id,
        candidate_container_id=seed.executor_container_id,
        candidate_image_id=seed.executor_image_id,
        incumbent_runtime_id=value.incumbent.executor_runtime_id,
        incumbent_container_id=value.incumbent.executor_container_id,
        observed_at=value.prepared_at,
    )
    journal.record_passive(proof, now=value.prepared_at)
    intent = CoreHandoverTransferIntent(
        handover_id=uuid4(),
        intent_id=uuid4(),
        preparation_sha256=value.sha256(),
        candidate_seed_sha256=seed.sha256(),
        passive_proof_sha256=proof.sha256(),
        generation=value.generation,
        consumed_at=value.prepared_at,
        deadline_at=value.prepared_at + timedelta(seconds=30),
    )
    if fault != "missing_intent":
        journal.record_transfer(intent)
    root.write(
        "bootstrap-handover.json",
        BootstrapHandover(
            incumbent_id=value.incumbent.executor_runtime_id,
            candidate_id=seed.executor_runtime_id,
            incumbent_container_id=value.incumbent.executor_container_id,
            candidate_container_id=seed.executor_container_id,
            incumbent_image_id=value.incumbent.executor_image_id,
            candidate_image_id=seed.executor_image_id,
            generation=value.generation,
            phase="restoring" if fault == "phase" else "verified",
            updated_at=value.prepared_at,
            deadline_at=value.deadline_at,
        ),
    )
    observation = source_physical.model_copy(
        update={
            "seed_sha256": seed.sha256(),
            "executor_runtime_id": seed.executor_runtime_id,
            "executor_container_id": seed.executor_container_id,
            "executor_image_id": seed.executor_image_id,
            "executor_started_at": value.prepared_at + timedelta(seconds=1)
            if fault == "restarted_candidate"
            else value.prepared_at,
        }
    )
    incumbent = CoreHandoverIncumbentObservation(
        preparation_sha256=value.sha256(),
        incumbent_runtime_id="wrong"
        if fault == "wrong_incumbent"
        else value.incumbent.executor_runtime_id,
        incumbent_container_id=value.incumbent.executor_container_id,
        incumbent_image_id=value.incumbent.executor_image_id,
        incumbent_started_at=value.prepared_at - timedelta(seconds=1),
        observed_at=datetime.now(UTC),
        incumbent_healthy=True,
    )

    def claim() -> None:
        with candidate_handover_ownership(settings, journal, observation, incumbent) as authority:
            assert authority.claim.executor_container_id == seed.executor_container_id
            assert authority.claim.generation == value.generation
            authority.verify()

    with bootstrap_ownership(
        Path(settings.upgrade_coordination_dir),
        value.incumbent,
        source_physical,
        settings.upgrade_executor_id,
    ):
        publish_handover_hold(settings, value)
        if fault == "held_incumbent":
            with pytest.raises(UpgradeConflict):
                claim()
    if fault == "held_incumbent":
        return
    if fault == "expired":
        with (
            pytest.raises(UpgradeUnavailable),
            candidate_handover_ownership(
                settings, journal, observation, incumbent, now=intent.deadline_at
            ),
        ):
            pytest.fail("expired candidate acquired a writer")
    elif fault:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
            claim()
    else:
        claim()
