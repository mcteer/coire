"""A new application Source cannot replace the independent executor provenance."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from upgrade_bootstrap_fixtures import prepared_bootstrap
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreBootstrapSeed,
    CoreRuntimeSelection,
    CoreSelectedService,
    DeploymentOperationHold,
    DeploymentOwnership,
    UpgradeDockerContainer,
    UpgradePhase,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor import selected_bootstrap as service
from coire_upgrade_executor.operation_supervisor import cache_accepted_plan


@pytest.mark.parametrize("fault", [None, "unacknowledged", "hold", "executor", "physical", "race"])
async def test_selected_source_requires_terminal_physical_proof_and_replays_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    plan = core_plan()
    seed, _ = prepared_bootstrap(plan.source)
    now = datetime.now(UTC)
    settings = Settings(
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    cache_accepted_plan(settings, plan)
    coordination = PrivateUpgradeStore(tmp_path / "coordination")
    manifest = plan.target.manifest
    assert manifest.compose_sha256 is not None
    assert manifest.host_release_key is not None
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity=settings.upgrade_executor_id,
        executor_runtime_id=seed.executor_runtime_id,
        executor_container_id="f" * 64 if fault == "executor" else seed.executor_container_id,
        boot_identity="isolated-linux-boot",
        generation=2,
        state="committed",
        stage="selected",
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=plan.source.manifest_sha256,
        committed_sha256=manifest.compose_sha256,
    )
    coordination.write("ownership.json", owner)
    selection = CoreRuntimeSelection(
        project="coire",
        release_id=plan.target.release_id,
        manifest_sha256=plan.target.manifest_sha256,
        compose_sha256=manifest.compose_sha256,
        host_release_key=manifest.host_release_key,
        owner_identity=owner.owner_identity,
        generation=owner.generation,
        selected_at=now,
        services=(
            CoreSelectedService(
                container=UpgradeDockerContainer(
                    container_id="a" * 64,
                    name="coire-coire-api-1",
                    project="coire",
                    service="coire-api",
                    image_id=manifest.services[0].image_id,
                    observed_at=now,
                    running=True,
                    health="healthy",
                    exit_code=0,
                    network_ids={"coire-internal": "b" * 64},
                )
            ),
        ),
    )
    coordination.write("runtime-selection.json", selection)
    hold = DeploymentOperationHold(
        project="coire",
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        plan_sha256=plan.authority_sha256(),
        source_sha256=plan.source.manifest_sha256,
        created_at=now,
        released=fault != "hold",
    )
    coordination.write("operation-hold.json", hold)
    receipt = SimpleNamespace(
        phase=UpgradePhase.SUCCEEDED, sequence=10, observed_sha256=plan.target.manifest_sha256
    )
    journal = SimpleNamespace(
        receipts=SimpleNamespace(
            latest=lambda: receipt,
            acknowledged_sequence=lambda: 9 if fault == "unacknowledged" else 10,
        )
    )
    monkeypatch.setattr(service, "ExecutorJournal", Mock(return_value=journal))
    monkeypatch.setattr(
        service,
        "verify_prepared_release",
        lambda _root, request: (
            plan.target if request.expected_sha256 == plan.target.manifest_sha256 else plan.source
        ),
    )

    async def observe() -> None:
        if fault == "physical":
            raise UpgradeUnavailable("not physically serving")
        if fault == "race":
            coordination.write("ownership.json", owner.model_copy(update={"generation": 3}))

    observer = Mock(return_value=SimpleNamespace(observe=AsyncMock(side_effect=observe)))
    monkeypatch.setattr(service, "InitialBootstrapObserver", observer)
    async with httpx.AsyncClient() as client:
        if fault is not None:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                await service.qualify_selected_bootstrap(settings, seed, client)
            assert not (tmp_path / "state/bootstrap/selected-seed.json").exists()
            return
        selected = await service.qualify_selected_bootstrap(settings, seed, client)
        assert selected.source == plan.target
        assert selected.executor_source == plan.source
        assert selected.executor_container_id == seed.executor_container_id
        assert selected.source_container_ids == {"coire-api": "a" * 64}
        store = PrivateUpgradeStore(tmp_path / "state/bootstrap")
        # Simulate a crash after immutable preparation, before alias publication.
        store.path("selected-seed.json").unlink()
        replay = await service.qualify_selected_bootstrap(settings, seed, client)
        assert replay == selected
        assert store.read("selected-seed.json", CoreBootstrapSeed) == selected
