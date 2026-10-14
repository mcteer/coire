"""Supervisor decisions preserve original epochs and keep interrupted candidates unready."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from test_handover_creation_journal import preparation

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreHandoverIncumbentObservation,
    CoreHandoverPreparation,
    CoreHandoverSummary,
    CoreHandoverTransferIntent,
    ExecutorPassiveReadiness,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.handover_candidate_supervisor import supervise_candidate_handover
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_source_supervisor import (
    recover_observed_preparation,
    supervise_transferred_handover,
)
from coire_upgrade_executor.passive import CandidatePassiveObserver


@pytest.mark.parametrize("committed", [False, True])
async def test_sql_commit_racing_observer_failure_cannot_restore_selected_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    from coire_upgrade_executor import supervisor

    settings, creation, _ = epoch(tmp_path)
    api = Mock(spec=ExecutorClient)
    api.handover_status = AsyncMock(
        return_value=Mock(phase="committed" if committed else "claimed")
    )
    monkeypatch.setattr(
        supervisor,
        "supervise_transferred_handover",
        AsyncMock(side_effect=UpgradeConflict("Candidate finalized during observation")),
    )
    restored = AsyncMock()
    monkeypatch.setattr(supervisor, "restore_prepared_handover", restored)
    stop = asyncio.Event()

    async def retire(*args: object) -> None:
        stop.set()

    retired = AsyncMock(side_effect=retire)
    monkeypatch.setattr(supervisor, "retire_incumbent", retired)
    async with httpx.AsyncClient(trust_env=False) as http:
        await supervisor.supervise_incumbent_handover(settings, creation, api, http, stop)
    api.handover_status.assert_awaited_once_with(creation.preparation)
    if committed:
        restored.assert_not_awaited()
        retired.assert_awaited_once()
    else:
        restored.assert_awaited_once()
        retired.assert_not_awaited()


def epoch(
    tmp_path: Path, *, aged: bool = False
) -> tuple[Settings, HandoverCreationJournal, PrivateUpgradeStore]:
    settings = Settings(
        upgrades_enabled=True,
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    prep = preparation()
    if aged:
        prep = prep.model_copy(
            update={
                "prepared_at": prep.prepared_at - timedelta(seconds=60),
                "deadline_at": prep.deadline_at - timedelta(seconds=60),
            }
        )
    bootstrap = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    creation = HandoverCreationJournal(bootstrap.root / "handover/2", prep)
    initial = creation.begin(now=prep.prepared_at)
    creation.observe(initial, "a" * 64, observed_at=prep.prepared_at, now=prep.prepared_at)
    seed = creation.candidate_seed()
    bootstrap.write("handover-preparation.json", prep)
    bootstrap.write(
        "bootstrap-handover.json",
        BootstrapHandover(
            incumbent_id=prep.incumbent.executor_runtime_id,
            incumbent_container_id=prep.incumbent.executor_container_id,
            incumbent_image_id=prep.incumbent.executor_image_id,
            candidate_id=seed.executor_runtime_id,
            candidate_container_id=seed.executor_container_id,
            candidate_image_id=seed.executor_image_id,
            generation=prep.generation,
            phase="verified",
            updated_at=prep.prepared_at,
            deadline_at=prep.deadline_at,
        ),
    )
    return settings, creation, bootstrap


@pytest.mark.parametrize("fault", ["not_captured", "expired", "incumbent_restarted"])
async def test_source_never_starts_stability_before_original_candidate_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    import coire_upgrade_executor.handover_source_supervisor as module

    settings, creation, bootstrap = epoch(tmp_path, aged=fault == "expired")
    prep, seed = creation.preparation, creation.retained_candidate_seed()
    proof = ExecutorPassiveReadiness(
        preparation_sha256=prep.sha256(),
        candidate_seed_sha256=seed.sha256(),
        generation=prep.generation,
        candidate_runtime_id=seed.executor_runtime_id,
        candidate_container_id=seed.executor_container_id,
        candidate_image_id=seed.executor_image_id,
        incumbent_runtime_id=prep.incumbent.executor_runtime_id,
        incumbent_container_id=prep.incumbent.executor_container_id,
        observed_at=prep.prepared_at,
    )
    creation.store.write("passive-proof.json", proof)
    at = datetime.now(UTC) - timedelta(seconds=31 if fault == "expired" else 0)
    transfer = CoreHandoverTransferIntent(
        handover_id=uuid4(),
        intent_id=uuid4(),
        preparation_sha256=prep.sha256(),
        candidate_seed_sha256=seed.sha256(),
        passive_proof_sha256=proof.sha256(),
        generation=prep.generation,
        consumed_at=at,
        deadline_at=at + timedelta(seconds=30),
    )
    creation.store.write("transfer-intent.json", transfer)
    before = bootstrap.path("bootstrap-handover.json").read_bytes()
    observer = Mock()
    observer.observe_incumbent = AsyncMock(
        return_value=CoreHandoverIncumbentObservation(
            preparation_sha256=prep.sha256(),
            incumbent_runtime_id=prep.incumbent.executor_runtime_id,
            incumbent_container_id=prep.incumbent.executor_container_id,
            incumbent_image_id=prep.incumbent.executor_image_id,
            incumbent_started_at=prep.prepared_at + timedelta(seconds=1)
            if fault == "incumbent_restarted"
            else prep.prepared_at - timedelta(seconds=1),
            observed_at=datetime.now(UTC),
            incumbent_healthy=True,
        )
    )
    monkeypatch.setattr(module, "HandoverObserver", Mock(return_value=observer))
    monkeypatch.setattr(module, "verify_executor_namespace", lambda seed: None)
    writer = Mock(side_effect=UpgradeUnavailable("candidate has not captured its kernel"))
    monkeypatch.setattr(module, "verify_candidate_writer", writer)
    started = AsyncMock()
    monkeypatch.setattr(module, "start_handover_stability", started)
    stop = asyncio.Event()

    async def stop_poll(event: asyncio.Event) -> None:
        event.set()

    monkeypatch.setattr(module, "poll", stop_poll)
    api = Mock(spec=ExecutorClient)
    api.plan = None
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
            await supervise_transferred_handover(settings, creation, api, http, stop)
    started.assert_not_awaited()
    api.decide_handover_commit.assert_not_called()
    api.handover_status.assert_not_called()
    assert bootstrap.path("bootstrap-handover.json").read_bytes() == before
    assert writer.call_count == (1 if fault == "not_captured" else 0)


async def test_interrupted_passive_candidate_never_contacts_api_or_captures_writer(
    tmp_path: Path,
) -> None:
    settings, creation, bootstrap = epoch(tmp_path)
    passive = Mock(spec=CandidatePassiveObserver)
    passive.journal, passive.preparation = creation, creation.preparation
    passive.seed = creation.retained_candidate_seed()
    api = Mock(spec=ExecutorClient)
    app = FastAPI()
    app.state.bootstrap_verified = True
    stop = asyncio.Event()
    stop.set()
    before = bootstrap.path("bootstrap-handover.json").read_bytes()
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(UpgradeConflict):
            await supervise_candidate_handover(app, settings, stop, passive, api, http)
    assert not app.state.bootstrap_verified
    assert app.state.bootstrap_authority is None and app.state.passive_observer is None
    assert bootstrap.path("bootstrap-handover.json").read_bytes() == before
    assert not api.mock_calls


@pytest.mark.parametrize("status", ["passive", "committed", "unavailable", "missing_prior"])
async def test_observed_creation_recovery_never_renews_an_expired_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    import coire_upgrade_executor.handover_source_supervisor as module

    prep = preparation()
    prep = prep.model_copy(
        update={
            "prepared_at": prep.prepared_at - timedelta(seconds=241),
            "deadline_at": prep.deadline_at - timedelta(seconds=241),
        }
    )
    settings = Settings(upgrades_enabled=True, upgrade_state_dir=str(tmp_path / "state"))
    bootstrap = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    bootstrap.write("handover-preparation.json", prep)
    creation = HandoverCreationJournal(bootstrap.root / "handover/2", prep)
    initial = creation.begin(now=prep.prepared_at)
    creation.dispatch(initial, now=prep.prepared_at)
    creation.observe(initial, "a" * 64, observed_at=prep.prepared_at, now=prep.prepared_at)
    if status == "missing_prior":
        bootstrap.write(
            "bootstrap-handover.json",
            BootstrapHandover(
                incumbent_id=prep.incumbent.executor_runtime_id,
                incumbent_container_id=prep.incumbent.executor_container_id,
                incumbent_image_id=prep.incumbent.executor_image_id,
                candidate_id="previous-candidate",
                candidate_container_id="b" * 64,
                candidate_image_id="sha256:" + "b" * 64,
                generation=1,
                phase="restored",
                updated_at=prep.prepared_at,
                deadline_at=prep.deadline_at,
            ),
        )
    assert not creation.store.path("candidate-seed.json").exists()
    monkeypatch.setattr(module, "verify_executor_namespace", lambda seed: None)
    api = Mock(spec=ExecutorClient)
    api.plan = None
    if status == "unavailable":
        api.handover_status = AsyncMock(side_effect=UpgradeUnavailable("status unavailable"))
    else:
        api.handover_status = AsyncMock(
            return_value=CoreHandoverSummary(
                handover_id=uuid4(),
                project="coire",
                generation=prep.generation,
                phase="passive" if status in {"passive", "missing_prior"} else "committed",
                preparation_sha256=prep.sha256(),
                candidate_release_id=prep.candidate_release.release_id,
                incumbent_image_id=prep.incumbent.executor_image_id,
                candidate_image_id=prep.candidate_image_id,
                prepared_at=prep.prepared_at,
                deadline_at=prep.deadline_at,
            )
        )
    if status == "passive":
        await recover_observed_preparation(settings, creation, api)
        current = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
        assert current is not None and current.phase == "passive"
        assert current.updated_at == prep.prepared_at and current.deadline_at == prep.deadline_at
        assert current.deadline_at < datetime.now(UTC)
        assert creation.retained_candidate_seed().prepared_at == prep.prepared_at
    else:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
            await recover_observed_preparation(settings, creation, api)
        if status == "missing_prior":
            retained = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
            assert retained is not None and retained.generation == 1
        else:
            assert not bootstrap.path("bootstrap-handover.json").exists()
        assert not creation.store.path("candidate-seed.json").exists()
    assert bootstrap.read("handover-preparation.json", CoreHandoverPreparation) == prep
    assert not creation.store.path("candidate-start.json").exists()
    assert not creation.store.path("transfer-intent.json").exists()
