"""A bootstrap heartbeat cannot invent readiness or overwrite another incarnation."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from upgrade_bootstrap_fixtures import bootstrap_heartbeat

from coire_api.auth import Principal, PrincipalKind
from coire_api.upgrades import bootstrap_heartbeat as service
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import CoreBootstrapHeartbeat
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "replay",
        "new_epoch",
        "actor",
        "host",
        "stale",
        "future",
        "registry",
        "prepared",
        "lower_epoch",
        "same_epoch_boot",
        "missing_provenance",
        "expired_verification",
    ],
)
async def test_bootstrap_heartbeat_preserves_original_reads_and_exact_source_incarnation(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    body = bootstrap_heartbeat()
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR,
        subject=body.executor_id,
        scopes=frozenset({"upgrades:execute"}),
    )
    retained = body
    owner = None
    if fault in {"replay", "new_epoch", "lower_epoch", "same_epoch_boot", "missing_provenance"}:
        owner = SimpleNamespace(
            identity=body.executor_id,
            source_release_id=body.seed.source.release_id,
            image_id=body.seed.executor_image_id,
            generation=2 if fault == "lower_epoch" else 1,
            heartbeat_at=body.physical.observed_at,
            bootstrap_ready=True,
            bootstrap_payload=None
            if fault == "missing_provenance"
            else retained.model_dump(mode="json"),
        )
    if fault == "actor":
        principal = principal.model_copy(update={"subject": "another-executor"})
    data = body.model_dump(mode="json")
    if fault == "host":
        data["lease"]["kernel_platform"] = "darwin"
    if fault in {"stale", "future"}:
        observed = body.physical.observed_at + timedelta(seconds=-6 if fault == "stale" else 1)
        data["physical"]["observed_at"] = observed.isoformat()
        data["physical"]["source"]["observed_at"] = observed.isoformat()
    if fault == "new_epoch":
        data["ownership"]["generation"] = 2
        observed = body.physical.observed_at + timedelta(microseconds=1)
        data["physical"]["observed_at"] = observed.isoformat()
        data["physical"]["source"]["observed_at"] = observed.isoformat()
    if fault == "same_epoch_boot":
        data["ownership"]["boot_identity"] = "changed-boot"
        data["lease"]["kernel_boot_identity"] = "changed-boot"
        observed = body.physical.observed_at + timedelta(microseconds=1)
        data["physical"]["observed_at"] = observed.isoformat()
        data["physical"]["source"]["observed_at"] = observed.isoformat()
    body = CoreBootstrapHeartbeat.model_validate(data)
    session = AsyncMock()
    session.add = Mock()
    session.get.return_value = owner
    registered = (
        body.seed.source.model_copy(update={"prepared_by": uuid4()})
        if fault == "registry"
        else body.seed.source
    )
    prepared = (
        body.seed.source.model_copy(update={"prepared_by": uuid4()})
        if fault == "prepared"
        else body.seed.source
    )
    monkeypatch.setattr(service, "operation_lock", AsyncMock())
    monkeypatch.setattr(service, "get_release", AsyncMock(return_value=registered))
    monkeypatch.setattr(service, "verify_prepared_release", Mock(return_value=prepared))
    audit = AsyncMock()
    monkeypatch.setattr(service, "write_principal_audit", audit)
    if fault == "expired_verification":
        now = datetime.now(UTC)
        monkeypatch.setattr(
            service,
            "datetime",
            SimpleNamespace(now=Mock(side_effect=[now, now + timedelta(seconds=6)])),
        )
    before = dict(vars(owner)) if owner else None
    if fault in {None, "replay", "new_epoch"}:
        result = await service.retain_bootstrap_heartbeat(
            session, principal, body, Settings(upgrades_enabled=True)
        )
        assert result == body
        written = owner if owner else session.add.call_args.args[0]
        assert written.heartbeat_at == body.physical.observed_at
        assert written.bootstrap_payload == body.model_dump(mode="json")
        assert written.bootstrap_ready
        assert audit.await_count == (0 if fault == "replay" else 1)
    else:
        with pytest.raises((UpgradeConflict, UpgradeForbidden, UpgradeUnavailable)):
            await service.retain_bootstrap_heartbeat(
                session, principal, body, Settings(upgrades_enabled=True)
            )
        session.add.assert_not_called()
        audit.assert_not_awaited()
        assert (dict(vars(owner)) if owner else None) == before


@pytest.mark.parametrize("fault", [None, "executor", "unfinished", "actor", "hold", "containers"])
async def test_source_transition_requires_completed_same_executor_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from upgrade_fixtures import core_plan

    from coire_core.models.upgrades import (
        CoreRuntimeSelection,
        CoreSelectedService,
        DeploymentOperationHold,
        UpgradeDockerContainer,
        UpgradePhase,
    )
    from coire_core.upgrade_journal import PrivateUpgradeStore

    plan = core_plan()
    old = bootstrap_heartbeat()
    old_data = old.model_dump(mode="json")
    old_data["seed"]["source"] = plan.source.model_dump(mode="json")
    from coire_core.models.upgrades import CoreBootstrapSeed

    old_seed = CoreBootstrapSeed.model_validate(old_data["seed"])
    old_data["physical"]["seed_sha256"] = old_seed.sha256()
    old_data["physical"]["source"]["release_id"] = str(plan.source.release_id)
    old_data["physical"]["source"]["source_sha256"] = plan.source.manifest_sha256
    old_data["ownership"]["source_sha256"] = plan.source.manifest_sha256
    old = CoreBootstrapHeartbeat.model_validate(old_data)
    data = old.model_dump(mode="json")
    data["seed"].update(
        source=plan.target.model_dump(mode="json"),
        executor_release=plan.source.model_dump(mode="json"),
        source_container_ids={"coire-api": "a" * 64},
        generation=3,
    )
    if fault == "executor":
        data["seed"]["executor_runtime_id"] = "foreign-executor"
        data["physical"]["executor_runtime_id"] = "foreign-executor"
        data["ownership"]["executor_runtime_id"] = "foreign-executor"
    seed = CoreBootstrapSeed.model_validate(data["seed"])
    data["physical"]["seed_sha256"] = seed.sha256()
    data["physical"]["source"].update(
        release_id=str(plan.target.release_id),
        source_sha256=plan.target.manifest_sha256,
        container_ids=seed.source_container_ids,
    )
    data["ownership"].update(source_sha256=plan.target.manifest_sha256, generation=3)
    body = CoreBootstrapHeartbeat.model_validate(data)
    settings = Settings(upgrade_coordination_dir=str(tmp_path / "coordination"))
    store = PrivateUpgradeStore(tmp_path / "coordination")
    now = old.physical.observed_at
    store.write(
        "operation-hold.json",
        DeploymentOperationHold(
            project="coire",
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            plan_sha256=plan.authority_sha256(),
            source_sha256=plan.source.manifest_sha256,
            created_at=now,
            released=fault != "hold",
        ),
    )
    target = plan.target.manifest
    assert target.compose_sha256 is not None and target.host_release_key is not None
    store.write(
        "runtime-selection.json",
        CoreRuntimeSelection(
            project="coire",
            release_id=plan.target.release_id,
            manifest_sha256=plan.target.manifest_sha256,
            compose_sha256=target.compose_sha256,
            host_release_key=target.host_release_key,
            owner_identity=old.executor_id,
            generation=2,
            selected_at=now,
            services=(
                CoreSelectedService(
                    container=UpgradeDockerContainer(
                        container_id="b" * 64 if fault == "containers" else "a" * 64,
                        name="coire-coire-api-1",
                        project="coire",
                        service="coire-api",
                        image_id=target.services[0].image_id,
                        observed_at=now,
                        running=True,
                        health="healthy",
                        exit_code=0,
                        network_ids={"coire-internal": "c" * 64},
                    )
                ),
            ),
        ),
    )
    operation = plan.operation.model_copy(
        update={
            "phase": UpgradePhase.SUCCEEDED,
            "finished_at": now,
            "receipt_sequence": 10,
        }
    )
    if fault == "unfinished":
        operation = plan.operation
    row = SimpleNamespace(
        execution_plan=plan.model_dump(mode="json"),
        payload=operation.model_dump(mode="json"),
        executor_identity="another-executor" if fault == "actor" else old.executor_id,
    )
    session = AsyncMock()
    session.get.return_value = row
    if fault is None:
        await service.verify_source_transition(session, old, body, settings)
    else:
        with pytest.raises(UpgradeConflict):
            await service.verify_source_transition(session, old, body, settings)
