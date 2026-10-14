"""Gateway serving evidence must identify its actual independently observed container."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from upgrade_bootstrap_fixtures import prepared_bootstrap
from upgrade_fixtures import core_plan

from coire_api.upgrades import gateway_smoke as service
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreRuntimeSelection,
    CoreSelectedService,
    DeploymentOperationHold,
    DeploymentOwnership,
    UpgradeDockerContainer,
    UpgradeSmokeRequest,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize(
    "fault", [None, "namespace", "generation", "stale", "wrong_release", "hold"]
)
def test_gateway_smoke_verifies_fresh_selected_api_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restored: bool, fault: str | None
) -> None:
    plan = core_plan()
    seed, _ = prepared_bootstrap(plan.source)
    release = plan.source if restored else plan.target
    manifest = release.manifest
    assert manifest.compose_sha256 is not None and manifest.host_release_key is not None
    settings = Settings(upgrade_coordination_dir=str(tmp_path / "coordination"))
    store = PrivateUpgradeStore(tmp_path / "coordination")
    now = datetime.now(UTC)
    at = now - timedelta(seconds=6) if fault == "stale" else now
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity=settings.upgrade_executor_id,
        executor_runtime_id=seed.executor_runtime_id,
        executor_container_id=seed.executor_container_id,
        boot_identity="isolated-linux-boot",
        generation=2,
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=plan.source.manifest_sha256,
    )
    store.write("ownership.json", owner)
    store.write(
        "operation-hold.json",
        DeploymentOperationHold(
            project="coire",
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            plan_sha256=plan.authority_sha256(),
            source_sha256=plan.source.manifest_sha256,
            created_at=now,
            released=fault == "hold",
        ),
    )
    observed = CoreRuntimeSelection(
        project="coire",
        release_id=release.release_id,
        manifest_sha256="f" * 64 if fault == "wrong_release" else release.manifest_sha256,
        compose_sha256=manifest.compose_sha256,
        host_release_key=manifest.host_release_key,
        owner_identity=owner.owner_identity,
        generation=1 if fault == "generation" else 2,
        selected_at=at,
        services=(
            CoreSelectedService(
                container=UpgradeDockerContainer(
                    container_id="a" * 64,
                    name="coire-coire-api-1",
                    project="coire",
                    service="coire-api",
                    image_id=manifest.services[0].image_id,
                    observed_at=at,
                    running=True,
                    health="healthy",
                    exit_code=0,
                    network_ids={"coire-internal": "b" * 64},
                )
            ),
        ),
    )
    store.write("observed-runtime.json", observed)
    monkeypatch.setattr(
        service, "api_container_namespace", lambda: "b" * 12 if fault == "namespace" else "a" * 12
    )
    request = UpgradeSmokeRequest(
        generation=plan.operation.generation, check="source" if restored else "gateway"
    )
    if fault is None:
        assert service.verify_smoke_runtime(settings, plan, request) == observed
    else:
        with pytest.raises(UpgradeConflict):
            service.verify_smoke_runtime(settings, plan, request)


def test_native_host_cannot_claim_core_api_smoke_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "linux_container_platform", lambda: False)
    with pytest.raises(UpgradeUnavailable):
        service.api_container_namespace()
