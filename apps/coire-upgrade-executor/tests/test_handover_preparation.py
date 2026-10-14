"""Incumbent preparation retains accepted authority before passive Docker creation."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from test_handover_creation_journal import preparation
from upgrade_bootstrap_fixtures import prepared_bootstrap

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreBootstrapPhysicalObservation,
    CoreHandoverCreation,
    CoreHandoverPreparation,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_preparation import prepare_passive_handover
from coire_upgrade_executor.ownership import bootstrap_ownership


@pytest.mark.parametrize("fault", [None, "conflicting", "lost_create"])
async def test_preparation_publishes_only_retained_intent_before_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    import coire_upgrade_executor.handover_preparation as module

    value = preparation()
    _, physical = prepared_bootstrap(value.incumbent.source)
    # Keep the exact seed captured by the accepted preparation.
    physical = physical.model_copy(update={"seed_sha256": value.incumbent.sha256()})
    settings = Settings(
        upgrades_enabled=True,
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    root = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    prior = value.model_copy(update={"candidate_runtime_id": "foreign"})
    if fault == "conflicting":
        root.write("handover-preparation.json", prior)

    async def create(_physical: CoreBootstrapPhysicalObservation) -> CoreHandoverCreation:
        assert root.read("handover-preparation.json", CoreHandoverPreparation) == value
        retained = HandoverCreationJournal.open_retained(
            root.root / "handover" / str(value.generation), value
        )
        assert retained.preparation == value
        if fault == "lost_create":
            raise UpgradeUnavailable("uncertain creation")
        effect = retained.begin(now=value.prepared_at)
        return retained.observe(
            effect, "a" * 64, observed_at=value.prepared_at, now=value.prepared_at
        )

    created = AsyncMock(side_effect=create)
    factory = Mock(return_value=Mock(create=created))
    monkeypatch.setattr(module, "HandoverDocker", factory)
    async with httpx.AsyncClient(trust_env=False) as http:
        with bootstrap_ownership(
            Path(settings.upgrade_coordination_dir),
            value.incumbent,
            physical,
            settings.upgrade_executor_id,
        ) as authority:
            if fault:
                with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                    await prepare_passive_handover(settings, value, authority, physical, http)
            else:
                seed = await prepare_passive_handover(settings, value, authority, physical, http)
                assert seed.executor_container_id == "a" * 64
                assert seed.source == value.incumbent.source
                assert root.read("selected-seed.json", type(seed)) is None
                passive = root.read("bootstrap-handover.json", BootstrapHandover)
                assert passive is not None and passive.phase == "passive"
                assert passive.updated_at == value.prepared_at
                assert passive.deadline_at == value.deadline_at
            authority.verify()
    if fault == "conflicting":
        factory.assert_not_called()
        assert root.read("handover-preparation.json", CoreHandoverPreparation) == prior
    else:
        created.assert_awaited_once()
        assert root.read("handover-preparation.json", CoreHandoverPreparation) == value


@pytest.mark.parametrize("advanced", [False, True])
async def test_recovery_metadata_or_new_writer_epoch_can_only_prohibit_candidate_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, advanced: bool
) -> None:
    from datetime import timedelta

    from coire_api.upgrades.handover_host_hold import publish_handover_hold
    from coire_core.models.upgrades import DeploymentHandoverHold
    from coire_core.upgrade_handover_unchanged import verify_unchanged_files
    from coire_upgrade_executor import handover_preparation as module
    from coire_upgrade_executor import handover_unchanged as unchanged_module

    value = preparation()
    if not advanced:
        value = value.model_copy(
            update={
                "prepared_at": value.prepared_at - timedelta(seconds=241),
                "deadline_at": value.deadline_at - timedelta(seconds=241),
            }
        )
    _, physical = prepared_bootstrap(value.incumbent.source)
    physical = physical.model_copy(update={"seed_sha256": value.incumbent.sha256()})
    settings = Settings(
        upgrades_enabled=True,
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    root = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    root.write("initial-seed.json", value.incumbent)
    monkeypatch.setattr(unchanged_module, "verify_executor_namespace", lambda _seed: None)
    effects = Mock(side_effect=AssertionError("recovery metadata cannot construct Docker effects"))
    monkeypatch.setattr(module, "HandoverDocker", effects)
    with bootstrap_ownership(
        tmp_path / "coordination", value.incumbent, physical, settings.upgrade_executor_id
    ) as original:
        publish_handover_hold(settings, value)
        hold = original.store.read("handover-hold.json", DeploymentHandoverHold)
        assert hold is not None
        if not advanced:
            creation = module.retain_unchanged_preparation(settings, value, original, physical)
            verify_unchanged_files(root, creation.store, value)
            original.verify()
    if advanced:
        with bootstrap_ownership(
            tmp_path / "coordination",
            value.incumbent,
            physical,
            settings.upgrade_executor_id,
            handover_admission=hold,
            bootstrap_root=root.root,
        ) as resumed:
            assert resumed.claim.generation == value.generation
            async with httpx.AsyncClient(trust_env=False) as http:
                with pytest.raises(UpgradeConflict, match="old candidate writer epoch"):
                    await prepare_passive_handover(settings, value, resumed, physical, http)
            creation = HandoverCreationJournal.open_retained(root.root / "handover/2", value)
            verify_unchanged_files(root, creation.store, value)
            resumed.verify()
    effects.assert_not_called()
    assert creation.current() is None
    assert not creation.store.path("candidate-seed.json").exists()
    assert not creation.store.path("candidate-start.json").exists()
    assert not creation.store.path("transfer-intent.json").exists()
    assert not root.path("bootstrap-handover.json").exists()
    assert not root.path("selected-seed.json").exists()
    assert root.read("handover-preparation.json", CoreHandoverPreparation) == value
