"""Managed Studio intent excludes host stack and credential mutations durably."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from upgrade_fixtures import plan

from coire_api.upgrades.host_hold import operation_hold
from coire_core.deploy import deployment_mutex, host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import DeploymentOperationHold, DeploymentOwnership
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


def test_studio_host_hold_survives_api_loss_until_verified_release(tmp_path: Path) -> None:
    value = plan()
    store = PrivateUpgradeStore(tmp_path / "coordination")
    store.write(
        "ownership.json",
        DeploymentOwnership(
            project="coire",
            owner_kind="host",
            owner_identity="initial-host",
            boot_identity=host_boot_identity(),
            generation=1,
            state="committed",
            stage="selected",
        ),
    )
    settings = Settings(upgrade_coordination_dir=str(store.root))
    operation_hold(settings, value)
    held = store.read("operation-hold.json", DeploymentOperationHold)
    operation_hold(settings, value)
    assert store.read("operation-hold.json", DeploymentOperationHold) == held
    assert not store.path("deployment.lease").exists()
    with (
        pytest.raises(UpgradeConflict, match="maintenance"),
        deployment_mutex(store, "coire", "host-up"),
    ):
        pytest.fail("host mutation reached an active Studio hold")
    assert not store.path("deployment.lease").exists()
    with pytest.raises(UpgradeConflict):
        operation_hold(settings, value.model_copy(update={"model_slug": "changed-model"}))
    operation_hold(settings, value, release=True)
    operation_hold(settings, value, release=True)
    with deployment_mutex(store, "coire", "host-up"):
        current = store.read("operation-hold.json", DeploymentOperationHold)
        assert current is not None and current.released
    with pytest.raises(UpgradeConflict):
        operation_hold(settings, value)


def test_missing_shared_bootstrap_does_not_create_an_unshared_hold(tmp_path: Path) -> None:
    settings = Settings(upgrade_coordination_dir=str(tmp_path / "absent"))
    with pytest.raises(UpgradeUnavailable):
        operation_hold(settings, plan())
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("persisted", [True, False])
async def test_orphan_hold_requires_serialized_authorized_database_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persisted: bool
) -> None:
    from coire_api.auth import Principal
    from coire_api.upgrades import host_hold

    value = plan()
    store = PrivateUpgradeStore(tmp_path / "coordination")
    store.write(
        "ownership.json",
        DeploymentOwnership(
            project="coire",
            owner_kind="host",
            owner_identity="initial-host",
            boot_identity=host_boot_identity(),
            generation=1,
            state="committed",
            stage="selected",
        ),
    )
    settings = Settings(upgrade_coordination_dir=str(store.root))
    operation_hold(settings, value)
    monkeypatch.setattr(host_hold, "operation_lock", AsyncMock())
    monkeypatch.setattr(host_hold, "authorize", AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr(host_hold, "write_principal_audit", audit)
    session = AsyncMock()
    session.get.return_value = object() if persisted else None
    await host_hold.reconcile_orphan_hold(session, Principal(), settings)
    held = store.read("operation-hold.json", DeploymentOperationHold)
    assert held is not None and held.released is (not persisted)
    assert audit.await_count == (0 if persisted else 1)


def test_idle_recovery_writer_can_accept_studio_hold_without_releasing_its_kernel_lock(
    tmp_path: Path,
) -> None:
    from upgrade_bootstrap_fixtures import prepared_bootstrap

    from coire_api.upgrades.host_hold import deployment_readiness
    from coire_upgrade_executor.ownership import bootstrap_ownership

    seed, physical = prepared_bootstrap()
    settings = Settings(
        upgrades_enabled=True, upgrade_coordination_dir=str(tmp_path / "coordination")
    )
    value = plan()
    with bootstrap_ownership(
        Path(settings.upgrade_coordination_dir), seed, physical, settings.upgrade_executor_id
    ) as authority:
        assert deployment_readiness(settings) == "ready"
        operation_hold(settings, value)
        assert deployment_readiness(settings) == "maintenance"
        authority.verify()
        held = authority.store.read("operation-hold.json", DeploymentOperationHold)
        assert held is not None and held.plan_sha256 == value.authority_sha256()
        operation_hold(settings, value, release=True)
        authority.verify()
        assert deployment_readiness(settings) == "ready"


async def test_orphan_acceptance_hold_can_close_beside_a_held_idle_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from upgrade_bootstrap_fixtures import prepared_bootstrap

    from coire_api.auth import Principal
    from coire_api.upgrades import host_hold
    from coire_upgrade_executor.ownership import bootstrap_ownership

    seed, physical = prepared_bootstrap()
    settings = Settings(
        upgrades_enabled=True, upgrade_coordination_dir=str(tmp_path / "coordination")
    )
    value = plan()
    monkeypatch.setattr(host_hold, "operation_lock", AsyncMock())
    monkeypatch.setattr(host_hold, "authorize", AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr(host_hold, "write_principal_audit", audit)
    session = AsyncMock()
    session.get.return_value = None
    with bootstrap_ownership(
        Path(settings.upgrade_coordination_dir), seed, physical, settings.upgrade_executor_id
    ) as authority:
        operation_hold(settings, value)
        await host_hold.reconcile_orphan_hold(session, Principal(), settings)
        held = authority.store.read("operation-hold.json", DeploymentOperationHold)
        assert held is not None and held.released
        authority.verify()
        audit.assert_awaited_once()


def test_host_kernel_acquisition_cannot_cross_admission_publication(tmp_path: Path) -> None:
    store = PrivateUpgradeStore(tmp_path)
    with store.ownership("deployment-admission.lock"):
        with pytest.raises(UpgradeConflict), deployment_mutex(store, "coire", "host-up"):
            pytest.fail("host entered while admission publication held the barrier")
        assert not store.path("deployment.lease").exists()
    with deployment_mutex(store, "coire", "host-up"):
        assert store.path("deployment.lease").exists()


def test_interrupted_idle_metadata_cannot_substitute_for_a_held_writer(tmp_path: Path) -> None:
    from upgrade_bootstrap_fixtures import prepared_bootstrap

    from coire_api.upgrades.host_hold import deployment_readiness
    from coire_upgrade_executor.ownership import bootstrap_ownership

    seed, physical = prepared_bootstrap()
    settings = Settings(
        upgrades_enabled=True, upgrade_coordination_dir=str(tmp_path / "coordination")
    )
    with (
        pytest.raises(RuntimeError),
        bootstrap_ownership(
            Path(settings.upgrade_coordination_dir), seed, physical, settings.upgrade_executor_id
        ),
    ):
        raise RuntimeError("simulated idle process death")
    assert deployment_readiness(settings) == "not_ready"
    with pytest.raises(UpgradeUnavailable):
        operation_hold(settings, plan())
    assert (
        PrivateUpgradeStore(Path(settings.upgrade_coordination_dir)).read(
            "operation-hold.json", DeploymentOperationHold
        )
        is None
    )


def test_handover_hold_outlives_incumbent_kernel_and_excludes_host_effects(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from upgrade_bootstrap_fixtures import prepared_bootstrap
    from upgrade_fixtures import core_plan

    from coire_api.upgrades.handover_host_hold import publish_handover_hold
    from coire_api.upgrades.host_hold import deployment_readiness
    from coire_core.models.upgrades import CoreHandoverPreparation, DeploymentHandoverHold
    from coire_upgrade_executor.ownership import bootstrap_ownership

    seed, physical = prepared_bootstrap()
    now = datetime.now(UTC)
    request = CoreHandoverPreparation(
        incumbent=seed,
        candidate_release=core_plan().target,
        candidate_runtime_id="candidate-executor",
        candidate_image_id="sha256:" + "f" * 64,
        generation=2,
        initiator_user_id=seed.source.prepared_by,
        idempotency_key=uuid4(),
        prepared_at=now,
        deadline_at=now + timedelta(seconds=120),
    )
    settings = Settings(
        upgrades_enabled=True, upgrade_coordination_dir=str(tmp_path / "coordination")
    )
    with bootstrap_ownership(
        Path(settings.upgrade_coordination_dir), seed, physical, settings.upgrade_executor_id
    ) as authority:
        publish_handover_hold(settings, request)
        held = authority.store.read("handover-hold.json", DeploymentHandoverHold)
        assert held is not None and not held.released
        publish_handover_hold(settings, request)
        assert authority.store.read("handover-hold.json", DeploymentHandoverHold) == held
        assert deployment_readiness(settings) == "maintenance"
        authority.verify()
    store = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    assert not store.path("deployment.lease").exists()
    with (
        pytest.raises(UpgradeConflict, match="handover"),
        deployment_mutex(store, "coire", "host-up"),
    ):
        pytest.fail("host effects crossed retained handover gap")
    assert not store.path("deployment.lease").exists()
    assert deployment_readiness(settings) == "maintenance"


async def test_orphan_handover_hold_requires_sql_absence_and_retained_incumbent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from upgrade_bootstrap_fixtures import prepared_bootstrap
    from upgrade_fixtures import core_plan

    from coire_api.auth import Principal
    from coire_api.upgrades import handover_host_hold
    from coire_core.models.upgrades import CoreHandoverPreparation, DeploymentHandoverHold
    from coire_upgrade_executor.ownership import bootstrap_ownership

    seed, physical = prepared_bootstrap()
    now = datetime.now(UTC)
    request = CoreHandoverPreparation(
        incumbent=seed,
        candidate_release=core_plan().target,
        candidate_runtime_id="candidate-executor",
        candidate_image_id="sha256:" + "f" * 64,
        generation=2,
        initiator_user_id=seed.source.prepared_by,
        idempotency_key=uuid4(),
        prepared_at=now,
        deadline_at=now + timedelta(seconds=120),
    )
    settings = Settings(
        upgrades_enabled=True,
        upgrade_coordination_dir=str(tmp_path / "coordination"),
        upgrade_state_dir=str(tmp_path / "state"),
    )
    session = AsyncMock()
    session.scalar.return_value = "retained-authority"
    monkeypatch.setattr(handover_host_hold, "operation_lock", AsyncMock(), raising=False)
    monkeypatch.setattr(handover_host_hold, "authorize", AsyncMock(), raising=False)
    audit = AsyncMock()
    monkeypatch.setattr(handover_host_hold, "write_principal_audit", audit, raising=False)
    with bootstrap_ownership(
        Path(settings.upgrade_coordination_dir), seed, physical, settings.upgrade_executor_id
    ) as authority:
        handover_host_hold.publish_handover_hold(settings, request)
        with pytest.raises(UpgradeConflict):
            await handover_host_hold.reconcile_orphan_handover_hold(session, Principal(), settings)
        held = authority.store.read("handover-hold.json", DeploymentHandoverHold)
        assert held is not None and not held.released
        audit.assert_not_awaited()
        session.scalar.return_value = None
        await handover_host_hold.reconcile_orphan_handover_hold(session, Principal(), settings)
        held = authority.store.read("handover-hold.json", DeploymentHandoverHold)
        assert held is not None and held.released
        audit.assert_awaited_once()
        authority.verify()
