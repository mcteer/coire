"""Handover metrics use original durable windows and never mutate recovery evidence."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from upgrade_bootstrap_fixtures import prepared_bootstrap
from upgrade_fixtures import core_plan

from coire_api.db import UpgradeHandoverRow
from coire_api.upgrades import handover_telemetry as telemetry
from coire_core.models.upgrades import (
    BootstrapHandover,
    BootstrapStabilityObservation,
    CoreHandoverCommitIntent,
    CoreHandoverPreparation,
    CoreHandoverRecoveryIntent,
    CoreHandoverTransferIntent,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("boundary", telemetry.BOUNDARIES)
async def test_original_deadlines_survive_later_collection_with_diagnostics_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    seed, _ = prepared_bootstrap()
    now = datetime.now(UTC)
    prepared = now - timedelta(seconds=241)
    prep = CoreHandoverPreparation(
        incumbent=seed,
        candidate_release=core_plan().target,
        candidate_runtime_id="candidate",
        candidate_image_id="sha256:" + "f" * 64,
        generation=2,
        initiator_user_id=seed.source.prepared_by,
        idempotency_key=uuid4(),
        prepared_at=prepared,
        deadline_at=prepared + timedelta(seconds=120),
    )
    settings = Settings(
        upgrades_enabled=True,
        diagnostics_enabled=False,
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    row = UpgradeHandoverRow(
        id=uuid4(),
        project="coire",
        generation=2,
        preparation_sha256=prep.sha256(),
        preparation_payload=prep.model_dump(mode="json"),
        created_at=prepared,
        phase="passive",
        active_slot=1,
    )
    expected = prep.deadline_at
    if boundary != "passive":
        transfer = CoreHandoverTransferIntent(
            handover_id=row.id,
            intent_id=uuid4(),
            preparation_sha256=prep.sha256(),
            candidate_seed_sha256="a" * 64,
            passive_proof_sha256="b" * 64,
            generation=2,
            consumed_at=prepared + timedelta(seconds=10),
            deadline_at=prepared + timedelta(seconds=40),
        )
        row.transfer_payload = transfer.model_dump(mode="json")
        row.phase, expected = "transfer_consumed", transfer.deadline_at
    bootstrap = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    bootstrap.write("handover-preparation.json", prep)
    phase = (
        "claimed"
        if boundary == "stability"
        else "restoring"
        if boundary == "restoration"
        else "verified"
    )
    started = prepared + timedelta(seconds=100) if phase == "claimed" else None
    current = BootstrapHandover(
        incumbent_id=seed.executor_runtime_id,
        incumbent_container_id=seed.executor_container_id,
        incumbent_image_id=seed.executor_image_id,
        candidate_id=prep.candidate_runtime_id,
        candidate_container_id="a" * 64,
        candidate_image_id=prep.candidate_image_id,
        generation=2,
        phase=phase,
        stability_started_at=started,
        updated_at=prepared,
        deadline_at=started + timedelta(seconds=120) if started else prep.deadline_at,
    )
    bootstrap.write("bootstrap-handover.json", current)
    if boundary == "stability":
        proof = BootstrapStabilityObservation(
            generation=current.generation,
            incumbent_id=current.incumbent_id,
            candidate_id=current.candidate_id,
            incumbent_container_id=current.incumbent_container_id,
            candidate_container_id=current.candidate_container_id,
            incumbent_image_id=current.incumbent_image_id,
            candidate_image_id=current.candidate_image_id,
            kernel_boot_identity="boot",
            candidate_started_at=prepared,
            observed_at=current.deadline_at - timedelta(seconds=1),
            incumbent_healthy=True,
            candidate_healthy=True,
            exclusive_writer_verified=True,
        )
        bootstrap.write("stability-observation.json", proof)
        expected = proof.observed_at + timedelta(seconds=10)
        assert telemetry.captured_deadline(row, settings) == (boundary, expected)
        bootstrap.path("stability-observation.json").unlink()
        assert started is not None
        assert telemetry.captured_deadline(row, settings) == (
            boundary,
            started + timedelta(seconds=10),
        )
        bootstrap.write("stability-observation.json", proof)
    elif boundary == "commit":
        decision = CoreHandoverCommitIntent(
            handover_id=row.id,
            decision_id=uuid4(),
            preparation_sha256=prep.sha256(),
            candidate_seed_sha256="a" * 64,
            stability_sha256="b" * 64,
            generation=2,
            decided_at=prepared + timedelta(seconds=222),
        )
        row.commit_payload = decision.model_dump(mode="json")
        row.phase, expected = "committing", decision.decided_at + timedelta(seconds=30)
    elif boundary == "restoration":
        journal = PrivateUpgradeStore(bootstrap.root / "handover/2")
        recovery = CoreHandoverRecoveryIntent(
            preparation_sha256=prep.sha256(),
            candidate_seed_sha256="a" * 64,
            generation=2,
            incumbent_container_id=seed.executor_container_id,
            candidate_container_id="a" * 64,
            detected_at=prepared + timedelta(seconds=240),
            deadline_at=prepared + timedelta(seconds=540),
        )
        journal.write("recovery-intent.json", recovery)
        row.phase, expected = "restoring", recovery.deadline_at
    before = bootstrap.path("bootstrap-handover.json").read_bytes()
    assert telemetry.captured_deadline(row, settings) == (boundary, expected)
    session = AsyncMock()
    session.scalars.return_value = SimpleNamespace(all=lambda: [row])
    recorder = Mock()
    monkeypatch.setattr(telemetry, "deadline_overdue", recorder)
    pending = Mock()
    monkeypatch.setattr(telemetry, "pending", pending)
    await telemetry.collect_handover_health(session, settings, now)
    recorder.set.assert_any_call(max(0, (now - expected).total_seconds()), {"boundary": boundary})
    later = now + timedelta(seconds=600)
    await telemetry.collect_handover_health(session, settings, later)
    recorder.set.assert_any_call((later - expected).total_seconds(), {"boundary": boundary})
    assert telemetry.captured_deadline(row, settings) == (boundary, expected)
    assert bootstrap.path("bootstrap-handover.json").read_bytes() == before
    assert not (tmp_path / "coordination").exists()
    session.commit.assert_not_awaited()
    session.flush.assert_not_awaited()


async def test_conflicting_handover_obligations_do_not_publish_a_clean_baseline(
    monkeypatch,
) -> None:
    session = AsyncMock()
    session.scalars.return_value = SimpleNamespace(all=lambda: [object(), object()])
    pending = Mock()
    monkeypatch.setattr(telemetry, "pending", pending)
    with pytest.raises(ValueError, match="conflicting"):
        await telemetry.collect_handover_health(session, Settings(), datetime.now(UTC))
    pending.set.assert_not_called()
