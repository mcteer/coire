"""Effect-free closure requires a held Source and prohibits all later candidate effects."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from test_handover_creation_journal import preparation
from upgrade_bootstrap_fixtures import prepared_bootstrap

from coire_api.upgrades.handover_host_hold import publish_handover_hold
from coire_api.upgrades.handover_unchanged import verify_unchanged_source
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreBootstrapPhysicalObservation,
    CoreHandoverPreparation,
    CoreHandoverSummary,
    CoreHandoverUnchangedRequest,
    DeploymentHandoverHold,
    UpgradeWire,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_docker import HandoverDocker
from coire_upgrade_executor.handover_preparation import archive_completed_handover
from coire_upgrade_executor.handover_unchanged import (
    abort_unchanged_handover,
    unchanged_completion_pending,
)
from coire_upgrade_executor.ownership import bootstrap_ownership
from coire_upgrade_executor.source_observer import InitialBootstrapObserver


@pytest.mark.parametrize(
    "fault", [None, "dispatched", "observed", "lost_ack", "foreign_ack", "release_interrupted"]
)
async def test_unchanged_failure_never_conceals_dispatch_or_releases_before_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    import coire_upgrade_executor.handover_unchanged as module

    prep = preparation()
    settings = Settings(
        upgrades_enabled=True,
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    bootstrap = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    bootstrap.write("handover-preparation.json", prep)
    creation = HandoverCreationJournal(bootstrap.root / "handover/2", prep)
    initial = creation.begin(now=prep.prepared_at)
    if fault == "dispatched":
        creation.dispatch(initial, now=prep.prepared_at)
    elif fault == "observed":
        creation.observe(initial, "a" * 64, observed_at=prep.prepared_at, now=prep.prepared_at)
    monkeypatch.setattr(module, "verify_executor_namespace", lambda seed: None)
    _, original = prepared_bootstrap(prep.incumbent.source)
    original = original.model_copy(update={"seed_sha256": prep.incumbent.sha256()})

    async def fresh() -> CoreBootstrapPhysicalObservation:
        _, physical = prepared_bootstrap(prep.incumbent.source)
        return physical.model_copy(update={"seed_sha256": prep.incumbent.sha256()})

    source = Mock(spec=InitialBootstrapObserver)
    source.observe = AsyncMock(side_effect=fresh)
    summary = CoreHandoverSummary(
        handover_id=uuid4(),
        project="coire",
        generation=prep.generation,
        phase="failed_unchanged",
        preparation_sha256=prep.sha256(),
        candidate_release_id=prep.candidate_release.release_id,
        incumbent_image_id=prep.incumbent.executor_image_id,
        candidate_image_id=prep.candidate_image_id,
        prepared_at=prep.prepared_at,
        deadline_at=prep.deadline_at,
    )
    api = Mock(spec=ExecutorClient)
    api.plan = None
    attempts = 0

    async def acknowledge(
        preparation: CoreHandoverPreparation, request: CoreHandoverUnchangedRequest
    ) -> CoreHandoverSummary:
        nonlocal attempts
        attempts += 1
        assert preparation == prep
        if attempts == 1:
            verify_unchanged_source(settings, prep, request)
        if fault == "lost_ack" and attempts == 1:
            raise UpgradeUnavailable("injected lost acknowledgment")
        return (
            summary.model_copy(update={"preparation_sha256": "f" * 64})
            if fault == "foreign_ack"
            else summary
        )

    api.acknowledge_handover_unchanged = AsyncMock(side_effect=acknowledge)
    coordination = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    with bootstrap_ownership(
        coordination.root, prep.incumbent, original, settings.upgrade_executor_id
    ) as authority:
        publish_handover_hold(settings, prep)
        write = authority.store.write

        def interrupted_release(key: str, value: UpgradeWire, *, replace: bool = True) -> None:
            if (
                key == "handover-hold.json"
                and isinstance(value, DeploymentHandoverHold)
                and value.released
            ):
                raise OSError("injected hold release interruption")
            write(key, value, replace=replace)

        if fault == "release_interrupted":
            with monkeypatch.context() as interruption:
                interruption.setattr(authority.store, "write", interrupted_release)
                with pytest.raises(OSError):
                    await abort_unchanged_handover(settings, creation, authority, source, api)
            assert creation.store.read("failure-ack.json", CoreHandoverSummary) == summary
        elif fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                await abort_unchanged_handover(settings, creation, authority, source, api)
        else:
            await abort_unchanged_handover(settings, creation, authority, source, api)
        hold = coordination.read("handover-hold.json", DeploymentHandoverHold)
        assert hold is not None
        if fault in {None, "lost_ack", "foreign_ack", "release_interrupted"}:
            docker = HandoverDocker.__new__(HandoverDocker)
            docker.journal = creation
            with pytest.raises(UpgradeForbidden, match="durably prohibits"):
                docker.verify_owner()
        if fault in {"lost_ack", "release_interrupted"}:
            assert not hold.released
            assert unchanged_completion_pending(settings, creation)
            await abort_unchanged_handover(settings, creation, authority, source, api)
            hold = coordination.read("handover-hold.json", DeploymentHandoverHold)
            assert hold is not None and hold.released
        if fault in {None, "lost_ack", "release_interrupted"}:
            assert not unchanged_completion_pending(settings, creation)
            assert creation.store.read("failure-ack.json", CoreHandoverSummary) == summary
            successor = prep.model_copy(
                update={
                    "generation": prep.generation + 1,
                    "candidate_runtime_id": "next-candidate",
                    "idempotency_key": uuid4(),
                }
            )
            archive_completed_handover(bootstrap, prep, successor)
            assert creation.store.read("completed-failure.json", CoreHandoverSummary) == summary
            archive_completed_handover(bootstrap, prep, successor)
        else:
            assert not hold.released
            assert creation.store.read("failure-ack.json", CoreHandoverSummary) is None
        if fault in {"dispatched", "observed"}:
            assert attempts == 0
            assert not creation.store.path("failure-intent.json").exists()
        authority.verify()
    assert not bootstrap.path("selected-seed.json").exists()
    assert not bootstrap.path("bootstrap-handover.json").exists()
