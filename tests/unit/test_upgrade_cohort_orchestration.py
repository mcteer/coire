"""Whole-cohort ordering and recovery use durable exact Source identities."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from upgrade_fixtures import core_plan

from coire_core.deploy import host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    COHORT_ORDER,
    DeploymentOwnership,
    UpgradeActivationConsume,
    UpgradeActivationGrant,
    UpgradeDockerContainer,
    UpgradeExecutionPlan,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.settings import Settings
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.docker import UpgradeDocker
from coire_upgrade_executor.executor import CoreExecutor
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.ownership import executor_ownership


def whole_cohort_plan(*, schema_changed: bool = False) -> UpgradeExecutionPlan:
    original = core_plan(schema_changed=schema_changed)
    source_manifest = original.source.manifest.model_copy(
        update={
            "services": tuple(
                original.source.manifest.services[0].model_copy(update={"service": name})
                for name in COHORT_ORDER
            )
        }
    )
    source = original.source.model_copy(
        update={"manifest": source_manifest, "manifest_sha256": source_manifest.sha256()}
    )
    target_manifest = original.target.manifest.model_copy(
        update={
            "services": tuple(
                role.model_copy(update={"image_id": "sha256:" + "b" * 64})
                for role in source_manifest.services
            ),
            "compatibility": original.target.manifest.compatibility.model_copy(
                update={"source_sha256s": (source.manifest_sha256,)}
            ),
        }
    )
    target = original.target.model_copy(
        update={"manifest": target_manifest, "manifest_sha256": target_manifest.sha256()}
    )
    return UpgradeExecutionPlan.model_validate(
        original.model_dump()
        | {
            "source": source,
            "target": target,
            "operation": original.operation.model_dump()
            | {
                "source_sha256": source.manifest_sha256,
                "target_sha256": target.manifest_sha256,
                "cohort": COHORT_ORDER,
            },
        }
    )


@pytest.mark.parametrize("failed_service", [None, *COHORT_ORDER])
async def test_every_service_failure_restores_changed_roles_in_reverse_order_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_service: str | None
) -> None:
    plan = whole_cohort_plan()
    journal = ExecutorJournal(tmp_path / "journal", plan)
    now = datetime.now(UTC)
    sources = {
        role.service: UpgradeDockerContainer(
            container_id=f"{index + 1:064x}",
            name="coire-" + role.service + "-1",
            service=role.service,
            project="coire",
            image_id=role.image_id,
            running=True,
            health="healthy",
            exit_code=0,
            network_ids={"coire-internal": "a" * 64},
            observed_at=now,
        )
        for index, role in enumerate(plan.source.manifest.services)
    }
    captured = journal.freeze_source(tuple(sources.values()))
    journal.activation(
        UpgradeActivationConsume(
            grant=UpgradeActivationGrant(
                job_id=plan.operation.job_id,
                generation=plan.operation.generation,
                boundary="runtime",
                grant_id=uuid4(),
                expires_at=now + timedelta(seconds=30),
            ),
            intent=UpgradeReceipt(
                job_id=plan.operation.job_id,
                generation=plan.operation.generation,
                sequence=1,
                effect_id="core-runtime-activation",
                phase=UpgradePhase.ACTIVATING,
                occurred_at=now,
                outcome="intent",
                observed_sha256=plan.source.manifest_sha256,
            ),
        )
    )
    settings = Settings(upgrade_executor_token="coire_upgrade_unit")
    docker, client = UpgradeDocker(settings, plan), ExecutorClient(settings)
    events = []

    async def dispatch(effect, current, authority, **kwargs):
        authority.verify()
        events.append((effect.action, effect.service))
        if effect.action == "start_candidate" and effect.service == failed_service:
            raise UpgradeConflict("synthetic candidate readiness failure")
        index = COHORT_ORDER.index(effect.service)
        candidate = "candidate" in effect.action
        observed = sources[effect.service].model_copy(
            update={
                "container_id": f"{index + 101:064x}" if candidate else effect.container_id,
                "name": "coire-" + effect.service + ("-candidate" if candidate else "-1"),
                "image_id": effect.image_id,
                "running": effect.action in {"start_source", "start_candidate"},
                "observed_at": datetime.now(UTC),
            }
        )
        current.observe(effect.effect_id, observed)
        if not candidate:
            sources[effect.service] = observed
        return observed

    for method in ("stop_source", "create_candidate", "candidate_lifecycle", "start_source"):
        monkeypatch.setattr(docker, method, AsyncMock(side_effect=dispatch))

    async def inspect_source(name, *_args, **_kwargs):
        return next(item for item in sources.values() if item.name == name).model_copy(
            update={"observed_at": datetime.now(UTC)}
        )

    monkeypatch.setattr(docker, "inspect_verified_source", AsyncMock(side_effect=inspect_source))
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="unit-source",
        boot_identity=host_boot_identity(),
        generation=1,
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=plan.source.manifest_sha256,
    )
    try:
        with executor_ownership(tmp_path / "coordination", owner, plan) as authority:
            executor = CoreExecutor(journal, docker, client, authority, tmp_path / "prepared")
            if failed_service is None:
                await executor.replace_cohort(dependency_states={})
                changed = COHORT_ORDER
            else:
                with pytest.raises(UpgradeConflict):
                    await executor.replace_cohort(dependency_states={})
                changed = COHORT_ORDER[: COHORT_ORDER.index(failed_service) + 1]
            assert [service for action, service in events if action == "stop_source"] == list(
                changed
            )
            events.clear()
            # Reconstruct from fsynced journals: recovery does not depend on former objects.
            restarted = ExecutorJournal(tmp_path / "journal", plan)
            executor = CoreExecutor(restarted, docker, client, authority, tmp_path / "prepared")
            await executor.restore_cohort(dependency_states={})
            assert [service for action, service in events if action == "start_source"] == list(
                reversed(changed)
            )
            assert [service for action, service in events if action == "stop_candidate"] == list(
                reversed(changed)
            )
            assert restarted.source_snapshot() == captured
            assert all(
                sources[item.service].container_id == item.container_id
                and sources[item.service].image_id == item.image_id
                and sources[item.service].running
                for item in captured.services
            )
            assert (
                restarted.state().recovery_deadline
                == restarted.capture_recovery().recovery_deadline
            )
            authority.verify()
    finally:
        await docker.close()
        await client.close()


@pytest.mark.parametrize("corrupt", [False, True])
async def test_unproved_migration_never_dispatches_any_cohort_replacement(
    tmp_path: Path, corrupt: bool
) -> None:
    plan = whole_cohort_plan(schema_changed=True)
    journal = ExecutorJournal(tmp_path / "journal", plan)
    journal.freeze_source(
        tuple(
            UpgradeDockerContainer(
                container_id=f"{index + 1:064x}",
                name="coire-" + role.service + "-1",
                service=role.service,
                project="coire",
                image_id=role.image_id,
                running=True,
                health="healthy",
                exit_code=0,
                network_ids={"coire-internal": "a" * 64},
                observed_at=datetime.now(UTC),
            )
            for index, role in enumerate(plan.source.manifest.services)
        )
    )
    if corrupt:
        journal.store.path("migration-proof.json").write_text("interrupted receipt")
    settings = Settings(upgrade_executor_token="coire_upgrade_unit")
    docker, client = UpgradeDocker(settings, plan), ExecutorClient(settings)
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="unit-source",
        boot_identity=host_boot_identity(),
        generation=1,
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=plan.source.manifest_sha256,
    )
    try:
        with executor_ownership(tmp_path / "coordination", owner, plan) as authority:
            executor = CoreExecutor(journal, docker, client, authority, tmp_path / "prepared")
            with pytest.raises(UpgradeUnavailable if corrupt else UpgradeForbidden):
                await executor.replace_cohort(dependency_states={})
            assert not list(journal.effects.root.glob("core-*.json"))
            assert journal.state().outage_deadline is None
            assert journal.state().recovery_captured_at is None
            authority.verify()
    finally:
        await docker.close()
        await client.close()


async def test_expired_recovery_replays_retained_failure_after_lost_ack(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from coire_core.models.upgrades import UpgradeReceiptAck
    from coire_upgrade_executor.workflow import recover_step

    plan = core_plan()
    journal = ExecutorJournal(tmp_path / "recovery-replay", plan)
    epoch = journal.capture_recovery(now=datetime.now(UTC) - timedelta(seconds=600))
    client = SimpleNamespace(
        bind=Mock(), receipts=AsyncMock(side_effect=UpgradeUnavailable("lost acknowledgment"))
    )
    executor = SimpleNamespace(
        journal=journal, client=client, authority=SimpleNamespace(verify=Mock())
    )
    receipt = await recover_step(executor)
    assert receipt is not None and receipt.phase is UpgradePhase.RECOVERY_REQUIRED
    assert journal.receipts.acknowledged_sequence() == 0
    with pytest.raises(UpgradeUnavailable, match="lost acknowledgment"):
        await recover_step(executor)
    assert journal.receipts.latest() == receipt
    assert journal.state().recovery_deadline == epoch.recovery_deadline
    assert journal.receipts.acknowledged_sequence() == 0
    client.receipts.side_effect = None
    client.receipts.return_value = UpgradeReceiptAck(
        job_id=receipt.job_id, generation=receipt.generation, sequence=receipt.sequence
    )
    assert await recover_step(executor) is None
    assert journal.receipts.acknowledged_sequence() == receipt.sequence
    assert journal.receipts.latest() == receipt
    assert journal.state().recovery_deadline == epoch.recovery_deadline


async def test_source_smoke_retry_refreshes_physical_census_without_renewing_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from coire_upgrade_executor import workflow

    deadline = datetime.now(UTC) + timedelta(seconds=120)
    state = SimpleNamespace(
        recovery_deadline=deadline,
        activations=[SimpleNamespace(grant=SimpleNamespace(boundary="runtime"))],
    )
    receipt = SimpleNamespace(phase=UpgradePhase.VERIFYING_SOURCE, outcome="intent")
    journal = SimpleNamespace(
        capture_recovery=Mock(return_value=state),
        receipts=SimpleNamespace(latest=Mock(return_value=receipt)),
    )
    fresh = False
    events = []

    async def inspect(**kwargs):
        nonlocal fresh
        events.append("inspect")
        fresh = True
        return SimpleNamespace()

    async def smoke(**kwargs):
        nonlocal fresh
        assert fresh, "expired census cannot authorize another serving attempt"
        assert kwargs == {"restored": True, "deadline": deadline}
        events.append("smoke")
        fresh = False
        raise UpgradeUnavailable("first serving reply lost")

    executor = SimpleNamespace(
        journal=journal, client=SimpleNamespace(smoke=smoke), restored_runtime=inspect
    )
    monkeypatch.setattr(workflow, "replay_receipts", AsyncMock())
    for _ in range(2):
        with pytest.raises(UpgradeUnavailable, match="reply lost"):
            await workflow.recover_step(executor)
        assert state.recovery_deadline == deadline
    assert events == ["inspect", "smoke", "inspect", "smoke"]
