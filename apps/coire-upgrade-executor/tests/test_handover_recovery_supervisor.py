"""Recovery driver rejects foreign authority before changing retained handover intent."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from test_handover_recovery_journal import recovery

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreHandoverCommitIntent,
    CoreHandoverRecoveryIntent,
    CoreHandoverSummary,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.handover_recovery_supervisor import restore_prepared_handover


@pytest.mark.parametrize("fault", ["disabled", "namespace", "application", "foreign", "committed"])
async def test_recovery_driver_rejects_before_recording_stop_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    import coire_upgrade_executor.handover_recovery_supervisor as module

    bootstrap = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    journal = recovery(tmp_path / "state/bootstrap/handover/2")
    prep = journal.creation.preparation
    seed = journal.creation.retained_candidate_seed()
    value = BootstrapHandover(
        incumbent_id=prep.incumbent.executor_runtime_id,
        incumbent_container_id=prep.incumbent.executor_container_id,
        incumbent_image_id=prep.incumbent.executor_image_id,
        candidate_id=seed.executor_runtime_id,
        candidate_container_id=seed.executor_container_id,
        candidate_image_id=seed.executor_image_id,
        generation=seed.generation,
        phase="verified",
        updated_at=prep.prepared_at,
        deadline_at=prep.deadline_at,
    )
    if fault == "foreign":
        value = value.model_copy(update={"candidate_id": "foreign"})
    elif fault == "committed":
        # Retained corruption is refused by strict decoding before mutation.
        value = value.model_copy(update={"phase": "committed"})
    bootstrap.write("bootstrap-handover.json", value)
    bootstrap.write("handover-preparation.json", prep)
    before = bootstrap.path("bootstrap-handover.json").read_bytes()
    api = Mock(spec=ExecutorClient)
    api.plan = object() if fault == "application" else None
    settings = Settings(
        upgrades_enabled=fault != "disabled", upgrade_state_dir=str(tmp_path / "state")
    )

    def namespace(seed: object) -> None:
        if fault == "namespace":
            raise UpgradeForbidden("foreign namespace")

    monkeypatch.setattr(module, "verify_executor_namespace", namespace)
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises((UpgradeConflict, UpgradeForbidden, UpgradeUnavailable, ValueError)):
            await restore_prepared_handover(settings, journal.creation, api, http)
    assert bootstrap.path("bootstrap-handover.json").read_bytes() == before
    assert journal.store.read("recovery-intent.json", CoreHandoverRecoveryIntent) is None
    assert not journal.store.path("restoration-ack.json").exists()


@pytest.mark.parametrize("status", ["committed", "accepted", "unavailable"])
async def test_lost_commit_reply_never_authorizes_candidate_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    import coire_upgrade_executor.handover_recovery_supervisor as module

    root = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    recovery_journal = recovery(root.root / "handover/2")
    creation = recovery_journal.creation
    prep, seed = creation.preparation, creation.retained_candidate_seed()
    handover = BootstrapHandover(
        incumbent_id=prep.incumbent.executor_runtime_id,
        incumbent_container_id=prep.incumbent.executor_container_id,
        incumbent_image_id=prep.incumbent.executor_image_id,
        candidate_id=seed.executor_runtime_id,
        candidate_container_id=seed.executor_container_id,
        candidate_image_id=seed.executor_image_id,
        generation=seed.generation,
        phase="verified",
        updated_at=prep.prepared_at,
        deadline_at=prep.deadline_at,
    )
    root.write("bootstrap-handover.json", handover)
    root.write("handover-preparation.json", prep)
    decision = CoreHandoverCommitIntent(
        handover_id=uuid4(),
        decision_id=uuid4(),
        preparation_sha256=prep.sha256(),
        candidate_seed_sha256=seed.sha256(),
        stability_sha256="b" * 64,
        generation=prep.generation,
        decided_at=prep.prepared_at,
    )
    creation.store.write("commit-decision.json", decision)
    api = Mock(spec=ExecutorClient)
    api.plan = None
    api.handover_status = AsyncMock()
    if status == "unavailable":
        api.handover_status.side_effect = UpgradeUnavailable("lost status response")
    else:
        api.handover_status.return_value = CoreHandoverSummary(
            handover_id=decision.handover_id,
            project="coire",
            generation=prep.generation,
            phase="committed" if status == "committed" else "accepted",
            preparation_sha256=prep.sha256(),
            candidate_release_id=prep.candidate_release.release_id,
            incumbent_image_id=prep.incumbent.executor_image_id,
            candidate_image_id=prep.candidate_image_id,
            prepared_at=prep.prepared_at,
            deadline_at=prep.deadline_at,
        )
    monkeypatch.setattr(module, "verify_executor_namespace", lambda seed: None)
    settings = Settings(upgrades_enabled=True, upgrade_state_dir=str(tmp_path / "state"))
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
            await restore_prepared_handover(settings, creation, api, http)
    api.handover_status.assert_awaited_once_with(prep)
    assert root.read("bootstrap-handover.json", BootstrapHandover) == handover
    retained = recovery_journal.current()
    assert retained is not None and retained.outcome == "intent"
    assert not creation.store.path("candidate-stopped-observation.json").exists()
