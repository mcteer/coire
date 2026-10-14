"""API success cannot precede exact independent Core runtime publication."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from upgrade_fixtures import core_plan

from coire_api.upgrades.host_hold import require_published_target
from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import (
    CoreRuntimeSelection,
    CoreSelectedService,
    DeploymentOwnership,
    DeploymentSelectionReceipt,
    UpgradeDockerContainer,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "fault",
    [None, "active", "runtime_missing", "foreign", "generation", "image", "origin", "retention"],
)
def test_success_requires_all_matching_published_selection_proofs(
    tmp_path: Path, fault: str | None
) -> None:
    plan = core_plan()
    settings = Settings(upgrade_coordination_dir=str(tmp_path / "coordination"))
    store = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    target = plan.target
    assert target.manifest.host_release_key is not None
    assert target.manifest.compose_sha256 is not None
    now = datetime.now(UTC)
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        state="active" if fault == "active" else "committed",
        stage="selected",
        owner_identity=settings.upgrade_executor_id,
        boot_identity="boot",
        generation=3,
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=plan.source.manifest_sha256,
        committed_sha256=target.manifest.compose_sha256,
    )
    protected = {
        release.manifest.host_release_key: release.manifest.compose_sha256
        for release in (plan.source, target)
    }
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in protected.items())
    if fault == "retention":
        protected.pop(plan.source.manifest.host_release_key)
    receipt = DeploymentSelectionReceipt.model_validate(
        {
            "project": owner.project,
            "owner_identity": owner.owner_identity,
            "boot_identity": owner.boot_identity,
            "generation": owner.generation,
            "release_key": target.manifest.host_release_key,
            "compose_sha256": target.manifest.compose_sha256,
            "protected_release_keys": tuple(sorted(protected)),
            "protected_compose_sha256s": protected,
            "selected_at": now,
            "managed_release_id": target.release_id,
            "manifest_sha256": target.manifest_sha256,
        }
    )
    role = target.manifest.services[0]
    runtime = CoreRuntimeSelection(
        project="coire",
        release_id=target.release_id,
        manifest_sha256=target.manifest_sha256,
        compose_sha256=target.manifest.compose_sha256,
        host_release_key=target.manifest.host_release_key,
        owner_identity="foreign" if fault == "foreign" else owner.owner_identity,
        generation=owner.generation + int(fault == "generation"),
        selected_at=now,
        services=(
            CoreSelectedService(
                container=UpgradeDockerContainer(
                    container_id="a" * 64,
                    name="coire-upgrade-" + plan.operation.job_id.lower() + "-coire-api",
                    project="coire",
                    service=role.service,
                    image_id="sha256:" + "f" * 64 if fault == "image" else role.image_id,
                    observed_at=now,
                    running=True,
                    health="healthy",
                    exit_code=0,
                    network_ids={"coire-internal": "b" * 64},
                ),
                origin_job_id=plan.operation.job_id,
                origin_generation=plan.operation.generation,
                origin_plan_sha256="f" * 64 if fault == "origin" else plan.authority_sha256(),
            ),
        ),
    )
    store.write("ownership.json", owner)
    store.write("selection.json", receipt)
    if fault != "runtime_missing":
        store.write("runtime-selection.json", runtime)
    before = {path.name: path.read_bytes() for path in store.root.iterdir()}
    if fault is None:
        require_published_target(settings, plan)
    else:
        with pytest.raises(UpgradeConflict, match="committed runtime"):
            require_published_target(settings, plan)
    assert {path.name: path.read_bytes() for path in store.root.iterdir()} == before
