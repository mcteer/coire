"""Public Core readiness cannot outlive its qualified physical and kernel owner."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from upgrade_bootstrap_fixtures import bootstrap_heartbeat
from upgrade_fixtures import core_plan

from coire_api.upgrades import core_targets
from coire_core.errors import UpgradeUnavailable
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "disabled",
        "maintenance",
        "missing",
        "unqualified",
        "identity",
        "generation",
        "image",
        "release",
        "timestamp",
        "stale",
        "future",
        "registered",
        "writer",
        "malformed",
    ],
)
async def test_core_ready_requires_fresh_accepted_source_and_current_writer(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    body = bootstrap_heartbeat()
    if fault in {"stale", "future"}:
        at = datetime.now(UTC) + timedelta(seconds=-6 if fault == "stale" else 1)
        physical = body.physical.model_copy(
            update={
                "observed_at": at,
                "source": body.physical.source.model_copy(update={"observed_at": at}),
            }
        )
        body = body.model_copy(update={"physical": physical})
    row = SimpleNamespace(
        bootstrap_ready=fault != "unqualified",
        identity="foreign" if fault == "identity" else body.executor_id,
        generation=body.ownership.generation + (1 if fault == "generation" else 0),
        image_id="sha256:" + "f" * 64 if fault == "image" else body.seed.executor_image_id,
        source_release_id=core_plan().target.release_id
        if fault == "release"
        else body.seed.source.release_id,
        heartbeat_at=body.physical.observed_at - timedelta(seconds=1)
        if fault == "timestamp"
        else body.physical.observed_at,
        bootstrap_payload={} if fault == "malformed" else body.model_dump(mode="json"),
    )
    session = AsyncMock()
    session.get.return_value = None if fault == "missing" else row
    registered = AsyncMock(
        return_value=core_plan().target if fault == "registered" else body.seed.source
    )
    monkeypatch.setattr(core_targets, "get_release", registered)
    writer = Mock(side_effect=UpgradeUnavailable("writer released") if fault == "writer" else None)
    monkeypatch.setattr(core_targets, "verify_current_writer", writer)
    result = await core_targets.core_target_status(
        session,
        Settings(upgrades_enabled=fault != "disabled"),
        "maintenance" if fault == "maintenance" else "ready",
    )
    assert (result.readiness == "ready") is (fault is None)
    assert result.bootstrap_ready is (fault is None)
    if fault is None:
        assert result.release_id == body.seed.source.release_id
        assert result.desired_sha256 == result.observed_sha256 == body.seed.source.manifest_sha256
        assert result.service_image_ids == body.physical.source.image_ids
        assert result.observed_at == body.physical.observed_at
        assert writer.call_count == 2
    else:
        assert result.blocking_reasons
        assert result.release_id is None
        if fault != "writer":
            writer.assert_not_called()


@pytest.mark.parametrize("fault", [None, "selected", "missing_seed", "lease", "owner", "released"])
def test_private_ready_proof_requires_selected_seed_exact_lease_and_held_writer(
    tmp_path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from coire_core.errors import UpgradeConflict
    from coire_core.upgrade_journal import PrivateUpgradeStore

    body = bootstrap_heartbeat()
    settings = Settings(
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    bootstrap = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    coordination = PrivateUpgradeStore(tmp_path / "coordination")
    if fault != "missing_seed":
        selected = (
            body.seed.model_copy(update={"executor_container_id": "f" * 64})
            if fault == "selected"
            else body.seed
        )
        bootstrap.write("selected-seed.json", selected)
    if fault != "lease":
        coordination.write("deployment.lease", body.lease)
    held = (
        None
        if fault == "released"
        else body.ownership.model_copy(update={"generation": body.ownership.generation + 1})
        if fault == "owner"
        else body.ownership
    )
    monkeypatch.setattr(core_targets, "idle_executor_owner", lambda *_args: held)
    before = sorted(path.name for path in coordination.root.iterdir())
    if fault:
        with pytest.raises(UpgradeConflict):
            core_targets.verify_current_writer(settings, body)
    else:
        core_targets.verify_current_writer(settings, body)
    assert sorted(path.name for path in coordination.root.iterdir()) == sorted(
        [*before, "deployment-admission.lock"]
    )
    assert not coordination.path("ownership.json").exists()


@pytest.mark.parametrize(
    "fault", [None, "pending", "decision", "source", "incompatible", "plan_missing"]
)
async def test_core_rollback_projects_only_exact_successful_retained_operation(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from upgrade_bootstrap_fixtures import prepared_bootstrap

    from coire_core.models.upgrades import DeploymentOperationHold, UpgradePhase

    plan = core_plan()
    body = bootstrap_heartbeat()
    seed, physical = prepared_bootstrap(plan.target)
    body = body.model_copy(
        update={
            "seed": seed,
            "physical": physical,
            "ownership": body.ownership.model_copy(
                update={
                    "source_sha256": plan.target.manifest_sha256,
                    "generation": 3,
                }
            ),
        }
    )
    parent = plan.operation.model_copy(
        update={
            "phase": UpgradePhase.ACCEPTED if fault == "pending" else UpgradePhase.SUCCEEDED,
            "finished_at": None if fault == "pending" else datetime.now(UTC),
            "receipt_sequence": 1,
        }
    )
    hold = DeploymentOperationHold(
        project="coire",
        job_id=parent.job_id,
        generation=parent.generation,
        source_sha256=parent.source_sha256,
        plan_sha256="f" * 64 if fault == "decision" else plan.authority_sha256(),
        created_at=parent.accepted_at,
        released=True,
    )
    monkeypatch.setattr(core_targets, "retained_operation", lambda *_args: hold)
    monkeypatch.setattr(
        core_targets,
        "get_operation",
        AsyncMock(
            return_value=SimpleNamespace(
                payload=parent.model_dump(mode="json"),
                execution_plan=None if fault == "plan_missing" else plan.model_dump(mode="json"),
            )
        ),
    )
    monkeypatch.setattr(
        core_targets,
        "get_release",
        AsyncMock(return_value=plan.target if fault == "source" else plan.source),
    )
    if fault == "incompatible":
        monkeypatch.setattr(core_targets, "release_pair_compatible", lambda *_args: False)
    retained_sha, job, blockers = await core_targets.rollback_projection(
        AsyncMock(), Settings(), body
    )
    if fault is None:
        assert retained_sha == plan.source.manifest_sha256 and job == parent.job_id
        assert not blockers
    else:
        assert job is None and blockers
        assert (retained_sha == plan.source.manifest_sha256) is (fault == "incompatible")


def test_readiness_never_creates_missing_private_authority(tmp_path) -> None:
    settings = Settings(
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    body = bootstrap_heartbeat()
    with pytest.raises(UpgradeUnavailable):
        core_targets.verify_current_writer(settings, body)
    assert core_targets.retained_operation(settings, body) is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "fault",
    [None, "released", "project", "owner", "source", "generation", "time", "container", "image"],
)
def test_retained_core_rollback_requires_exact_selected_physical_cohort(
    tmp_path, fault: str | None
) -> None:
    from upgrade_bootstrap_fixtures import prepared_bootstrap

    from coire_core.errors import UpgradeConflict
    from coire_core.models.upgrades import (
        CoreRuntimeSelection,
        CoreSelectedService,
        DeploymentOperationHold,
        UpgradeDockerContainer,
    )
    from coire_core.upgrade_journal import PrivateUpgradeStore

    plan = core_plan()
    body = bootstrap_heartbeat()
    seed, physical = prepared_bootstrap(plan.target)
    body = body.model_copy(
        update={
            "seed": seed,
            "physical": physical,
            "ownership": body.ownership.model_copy(
                update={"generation": 3, "source_sha256": plan.target.manifest_sha256}
            ),
        }
    )
    settings = Settings(upgrade_coordination_dir=str(tmp_path / "coordination"))
    store = PrivateUpgradeStore(tmp_path / "coordination")
    hold = DeploymentOperationHold(
        project="foreign" if fault == "project" else "coire",
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        source_sha256=plan.source.manifest_sha256,
        plan_sha256=plan.authority_sha256(),
        created_at=plan.operation.accepted_at,
        released=fault != "released",
    )
    at = (
        physical.observed_at + timedelta(seconds=1)
        if fault == "time"
        else physical.observed_at - timedelta(seconds=1)
    )
    assert plan.target.manifest.compose_sha256 is not None
    assert plan.target.manifest.host_release_key is not None
    selected = CoreRuntimeSelection(
        project="coire",
        release_id=seed.source.release_id,
        manifest_sha256="f" * 64 if fault == "source" else seed.source.manifest_sha256,
        compose_sha256=seed.source.manifest.compose_sha256,
        host_release_key=seed.source.manifest.host_release_key,
        owner_identity="foreign" if fault == "owner" else body.executor_id,
        generation=3 if fault == "generation" else 2,
        selected_at=at,
        services=tuple(
            CoreSelectedService(
                container=UpgradeDockerContainer(
                    container_id="b" * 64
                    if fault == "container"
                    else seed.source_container_ids[role.service],
                    name="coire-upgrade-" + plan.operation.job_id.lower() + "-" + role.service,
                    project="coire",
                    service=role.service,
                    image_id="sha256:" + "f" * 64 if fault == "image" else role.image_id,
                    observed_at=at,
                    running=True,
                    health="healthy",
                    exit_code=0,
                    network_ids={"coire-internal": "b" * 64},
                ),
                origin_job_id=plan.operation.job_id,
                origin_generation=plan.operation.generation,
                origin_plan_sha256=plan.authority_sha256(),
            )
            for role in seed.source.manifest.services
        ),
    )
    store.write("operation-hold.json", hold)
    store.write("runtime-selection.json", selected)
    before = {path.name: path.read_bytes() for path in store.root.iterdir()}
    if fault:
        with pytest.raises(UpgradeConflict):
            core_targets.retained_operation(settings, body)
    else:
        assert core_targets.retained_operation(settings, body) == hold
    assert {path.name: path.read_bytes() for path in store.root.iterdir()} == before
